from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from supabase import create_client, Client
import fitz
import os
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from aiogram.types import FSInputFile, ContentType, Update
from contextlib import asynccontextmanager, suppress
import asyncio
import qrcode
from io import BytesIO
import random
import string
from PIL import Image

# ------------ CONFIG ------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
# Si no tienes BASE_URL, usa el de Render que pusiste antes:
if not BASE_URL:
    BASE_URL = "https://morelosgobmovilidad-y-transporte.onrender.com"

OUTPUT_DIR = "documentos"
PLANTILLA_PDF = "morelos_hoja1_imagen.pdf"  # hoja 1 es index 0
PLANTILLA_BUENO = "morelosvergas1.pdf"

# Precio del permiso
PRECIO_PERMISO = 200

# Coordenadas Morelos (en puntos)
coords_morelos = {
    "folio": (665, 282, 18, (1, 0, 0)),
    "placa": (200, 200, 60, (0, 0, 0)),
    "fecha": (200, 340, 14, (0, 0, 0)),
    "vigencia": (600, 340, 14, (0, 0, 0)),
    "marca": (110, 425, 14, (0, 0, 0)),
    "serie": (460, 420, 14, (0, 0, 0)),
    "linea": (110, 455, 14, (0, 0, 0)),
    "motor": (460, 445, 14, (0, 0, 0)),
    "anio": (110, 485, 14, (0, 0, 0)),
    "color": (460, 395, 14, (0, 0, 0)),
    "tipo": (510, 470, 14, (0, 0, 0)),
    "nombre": (150, 370, 14, (0, 0, 0)),
}

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ------------ SUPABASE ------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------ BOT (Aiogram v3) ------------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ------------ TIMER MANAGEMENT - 12 HORAS CON TIMERS INDEPENDIENTES ------------
timers_activos = {}   # {folio: {"task": task, "user_id": user_id, "start_time": datetime}}
user_folios = {}      # {user_id: [lista_de_folios_activos]}
pending_comprobantes = {}

# ------------ QR DINÁMICO (apunta a /consulta/{folio}) ------------
def generar_qr_dinamico_morelos(folio: str):
    try:
        url_directa = f"{BASE_URL}/consulta/{folio}"
        qr = qrcode.QRCode(
            version=2,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=4,
            border=1
        )
        qr.add_data(url_directa)
        qr.make(fit=True)
        img_qr = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        print(f"[QR MORELOS] Generado para folio {folio} -> {url_directa}")
        return img_qr, url_directa
    except Exception as e:
        print(f"[ERROR QR MORELOS] {e}")
        return None, None

async def eliminar_folio_automatico(folio: str):
    try:
        user_id = None
        if folio in timers_activos:
            user_id = timers_activos[folio]["user_id"]

        supabase.table("folios_registrados").delete().eq("folio", folio).execute()
        supabase.table("borradores_registros").delete().eq("folio", folio).execute()

        if user_id:
            await bot.send_message(
                user_id,
                f"**⏰ TIEMPO AGOTADO**\n\n"
                f"**El folio {folio} ha sido eliminado del sistema por falta de pago.**\n\n"
                f"Para tramitar un nuevo permiso utilize **/permiso**",
                parse_mode="Markdown"
            )

        limpiar_timer_folio(folio)
    except Exception as e:
        print(f"Error eliminando folio {folio}: {e}")

async def enviar_recordatorio(folio: str, minutos_restantes: int):
    try:
        if folio not in timers_activos:
            return
        user_id = timers_activos[folio]["user_id"]
        await bot.send_message(
            user_id,
            f"**⚡ RECORDATORIO DE PAGO MORELOS**\n\n"
            f"**Folio:** {folio}\n"
            f"**Tiempo restante:** {minutos_restantes} minutos\n"
            f"**Monto:** El costo es el mismo de siempre\n\n"
            f"**📸 Envíe su comprobante de pago (imagen) para validar el trámite.**",
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"Error enviando recordatorio para folio {folio}: {e}")

async def iniciar_timer_pago(user_id: int, folio: str):
    async def timer_task():
        print(f"[TIMER] Iniciado para folio {folio}, usuario {user_id} - 12 HORAS")
        for horas in [2, 4, 6, 8, 10]:
            await asyncio.sleep(2 * 60 * 60)
            if folio not in timers_activos:
                print(f"[TIMER] Cancelado para folio {folio}")
                return
            horas_restantes = 12 - horas
            await enviar_recordatorio(folio, horas_restantes * 60)
        await asyncio.sleep(1.5 * 60 * 60)  # faltan 30 min
        if folio in timers_activos:
            await enviar_recordatorio(folio, 30)
        await asyncio.sleep(30 * 60)
        if folio in timers_activos:
            print(f"[TIMER] Expirado para folio {folio} después de 12 HORAS")
            await eliminar_folio_automatico(folio)

    task = asyncio.create_task(timer_task())
    timers_activos[folio] = {"task": task, "user_id": user_id, "start_time": datetime.now()}
    user_folios.setdefault(user_id, []).append(folio)
    print(f"[SISTEMA] Timer 12h iniciado para folio {folio}. Activos: {len(timers_activos)}")

def cancelar_timer_folio(folio: str):
    if folio in timers_activos:
        timers_activos[folio]["task"].cancel()
        user_id = timers_activos[folio]["user_id"]
        del timers_activos[folio]
        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]:
                del user_folios[user_id]
        print(f"[SISTEMA] Timer cancelado para folio {folio}. Restantes: {len(timers_activos)}")

def limpiar_timer_folio(folio: str):
    if folio in timers_activos:
        user_id = timers_activos[folio]["user_id"]
        del timers_activos[folio]
        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]:
                del user_folios[user_id]

def obtener_folios_usuario(user_id: int) -> list:
    return user_folios.get(user_id, [])

# ------------ FOLIO SYSTEM CON PREFIJO 345 ------------
folio_counter = {"count": 1}

def inicializar_folio_desde_supabase():
    try:
        response = supabase.table("folios_registrados") \
            .select("folio") \
            .eq("entidad", "morelos") \
            .order("folio", desc=True) \
            .limit(1) \
            .execute()

        if response.data:
            ultimo_folio = response.data[0]["folio"]
            if ultimo_folio.startswith("345") and len(ultimo_folio) > 3:
                try:
                    numero = int(ultimo_folio[3:])
                    folio_counter["count"] = numero + 1
                    print(f"[INFO] Folio inicializado: {ultimo_folio}, siguiente: 345{folio_counter['count']}")
                except ValueError:
                    print("[ERROR] Formato inválido en BD, iniciando 3451")
                    folio_counter["count"] = 1
            else:
                print("[INFO] No hay folios 345, iniciando 3451")
                folio_counter["count"] = 1
        else:
            print("[INFO] Sin folios, iniciando 3451")
            folio_counter["count"] = 1

        print(f"[SISTEMA] Próximo folio: 345{folio_counter['count']}")
    except Exception as e:
        print(f"[ERROR CRÍTICO] init folio: {e}")
        folio_counter["count"] = 1

def generar_folio_automatico() -> tuple:
    max_intentos = 5
    for _ in range(max_intentos):
        folio = f"345{folio_counter['count']}"
        try:
            response = supabase.table("folios_registrados").select("folio").eq("folio", folio).execute()
            if response.data:
                folio_counter["count"] += 1
                continue
            folio_counter["count"] += 1
            print(f"[SUCCESS] Folio generado: {folio}")
            return folio, True, ""
        except Exception as e:
            print(f"[ERROR] Verificando folio {folio}: {e}")
            folio_counter["count"] += 1
            continue
    return "", False, "Sistema sobrecargado, no se pudo generar folio único."

def generar_placa_digital():
    archivo = "placas_digitales.txt"
    abc = string.ascii_uppercase
    try:
        if not os.path.exists(archivo):
            with open(archivo, "w") as f:
                f.write("GSR1989\n")
        with open(archivo, "r") as f:
            ultimo = f.read().strip().split("\n")[-1]
        pref, num = ultimo[:3], int(ultimo[3:])
        if num < 9999:
            nuevo = f"{pref}{num+1:04d}"
        else:
            l1, l2, l3 = list(pref)
            i3 = abc.index(l3)
            if i3 < 25:
                l3 = abc[i3+1]
            else:
                i2 = abc.index(l2)
                if i2 < 25:
                    l2 = abc[i2+1]; l3 = "A"
                else:
                    l1 = abc[(abc.index(l1)+1) % 26]; l2 = l3 = "A"
            nuevo = f"{l1}{l2}{l3}0000"
        with open(archivo, "a") as f:
            f.write(nuevo + "\n")
        return nuevo
    except Exception as e:
        print(f"[ERROR] Placa digital: {e}")
        letras = ''.join(random.choices(abc, k=3))
        numeros = ''.join(random.choices('0123456789', k=4))
        return f"{letras}{numeros}"

# ------------ FSM STATES ------------
class PermisoForm(StatesGroup):
    marca = State()
    linea = State()
    anio = State()
    serie = State()
    motor = State()
    color = State()
    tipo = State()
    nombre = State()

# ------------ PDF FUNCTIONS (QR en HOJA 1, fechas dd/mm/yyyy) ------------
def generar_pdf_principal(datos: dict) -> tuple:
    try:
        doc = fitz.open(PLANTILLA_PDF)
        pg = doc[0]

        # Texto en HOJA 1
        pg.insert_text(coords_morelos["folio"][:2],    datos["folio"],    fontsize=coords_morelos["folio"][2],    color=coords_morelos["folio"][3])
        pg.insert_text(coords_morelos["placa"][:2],    datos["placa"],    fontsize=coords_morelos["placa"][2],    color=coords_morelos["placa"][3])
        pg.insert_text(coords_morelos["fecha"][:2],    datos["fecha"],    fontsize=coords_morelos["fecha"][2],    color=coords_morelos["fecha"][3])
        pg.insert_text(coords_morelos["vigencia"][:2], datos["vigencia"], fontsize=coords_morelos["vigencia"][2], color=coords_morelos["vigencia"][3])
        pg.insert_text(coords_morelos["marca"][:2],    datos["marca"],    fontsize=coords_morelos["marca"][2],    color=coords_morelos["marca"][3])
        pg.insert_text(coords_morelos["serie"][:2],    datos["serie"],    fontsize=coords_morelos["serie"][2],    color=coords_morelos["serie"][3])
        pg.insert_text(coords_morelos["linea"][:2],    datos["linea"],    fontsize=coords_morelos["linea"][2],    color=coords_morelos["linea"][3])
        pg.insert_text(coords_morelos["motor"][:2],    datos["motor"],    fontsize=coords_morelos["motor"][2],    color=coords_morelos["motor"][3])
        pg.insert_text(coords_morelos["anio"][:2],     datos["anio"],     fontsize=coords_morelos["anio"][2],     color=coords_morelos["anio"][3])
        pg.insert_text(coords_morelos["color"][:2],    datos["color"],    fontsize=coords_morelos["color"][2],    color=coords_morelos["color"][3])
        pg.insert_text(coords_morelos["tipo"][:2],     datos["tipo"],     fontsize=coords_morelos["tipo"][2],     color=coords_morelos["tipo"][3])
        pg.insert_text(coords_morelos["nombre"][:2],   datos["nombre"],   fontsize=coords_morelos["nombre"][2],   color=coords_morelos["nombre"][3])

        # QR DINÁMICO en HOJA 1
        img_qr, url_qr = generar_qr_dinamico_morelos(datos["folio"])
        if img_qr:
            buf = BytesIO()
            img_qr.save(buf, format="PNG"); buf.seek(0)
            qr_pix = fitz.Pixmap(buf.read())
            rect_qr = fitz.Rect(665, 282, 665 + 70.87, 282 + 70.87)  # ≈ 2.5 cm
            pg.insert_image(rect_qr, pixmap=qr_pix, overlay=True)
            print(f"[QR MORELOS] QR dinámico insertado en HOJA 1: {url_qr}")
        else:
            texto_qr_fallback = (
                f"FOLIO: {datos['folio']}\n"
                f"NOMBRE: {datos['nombre']}\n"
                f"MARCA: {datos['marca']}\n"
                f"LINEA: {datos['linea']}\n"
                f"AÑO: {datos['anio']}\n"
                f"SERIE: {datos['serie']}\n"
                f"MOTOR: {datos['motor']}\n"
                f"PERMISO MORELOS DIGITAL"
            )
            qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=10, border=2)
            qr.add_data(texto_qr_fallback); qr.make(fit=True)
            qr_img = qr.make_image(fill_color="black", back_color="white")
            buffer = BytesIO(); qr_img.save(buffer, format="PNG"); buffer.seek(0)
            rect_qr = fitz.Rect(665, 282, 665 + 70.87, 282 + 70.87)
            pg.insert_image(rect_qr, stream=buffer.read())
            print(f"[QR MORELOS] QR fallback (texto) en HOJA 1")

        filename = f"{OUTPUT_DIR}/{datos['folio']}_morelos.pdf"
        doc.save(filename); doc.close()
        return filename, True, ""
    except Exception as e:
        error_msg = f"Error generando PDF principal: {str(e)}"
        print(f"[ERROR PDF] {error_msg}")
        return "", False, error_msg

def generar_pdf_bueno(folio: str, numero_serie: str, nombre: str) -> tuple:
    try:
        doc = fitz.open(PLANTILLA_BUENO)
        page = doc[0]
        ahora = datetime.now()
        page.insert_text((155, 245), nombre.upper(), fontsize=18, fontname="helv")
        page.insert_text((1045, 205), folio, fontsize=20, fontname="helv")
        page.insert_text((1045, 275), ahora.strftime("%d/%m/%Y"), fontsize=20, fontname="helv")
        page.insert_text((1045, 348), ahora.strftime("%H:%M:%S"), fontsize=20, fontname="helv")
        filename = f"{OUTPUT_DIR}/{folio}.pdf"
        doc.save(filename); doc.close()
        return filename, True, ""
    except Exception as e:
        error_msg = f"Error generando PDF comprobante: {str(e)}"
        print(f"[ERROR PDF] {error_msg}")
        return "", False, error_msg

# ------------ DATABASE ------------
def guardar_en_database(datos: dict, fecha_iso: str, fecha_ven_iso: str, user_id: int, username: str) -> tuple:
    try:
        supabase.table("folios_registrados").insert({
            "folio": datos["folio"],
            "marca": datos["marca"],
            "linea": datos["linea"],
            "anio": datos["anio"],
            "numero_serie": datos["serie"],
            "numero_motor": datos["motor"],
            "color": datos["color"],
            "nombre": datos["nombre"],
            "fecha_expedicion": fecha_iso,
            "fecha_vencimiento": fecha_ven_iso,
            "entidad": "morelos",
            "estado": "PENDIENTE",
            "user_id": user_id,
            "username": username or "Sin username"
        }).execute()

        supabase.table("borradores_registros").insert({
            "folio": datos["folio"],
            "entidad": "Morelos",
            "numero_serie": datos["serie"],
            "marca": datos["marca"],
            "linea": datos["linea"],
            "numero_motor": datos["motor"],
            "anio": datos["anio"],
            "color": datos["color"],
            "fecha_expedicion": fecha_iso,
            "fecha_vencimiento": fecha_ven_iso,
            "contribuyente": datos["nombre"],
            "estado": "PENDIENTE",
            "user_id": user_id
        }).execute()
        return True, ""
    except Exception as e:
        error_msg = f"Error guardando en base de datos: {str(e)}"
        print(f"[ERROR DB] {error_msg}")
        return False, error_msg

# ------------ HANDLERS ------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    try:
        await state.clear()
        await message.answer(
            "**🏛️ Sistema Digital de Permisos del Estado de Morelos**\n"
            "Plataforma oficial para la gestión de trámites vehiculares\n\n"
            "**💰 Inversión del servicio:** El costo es el mismo de siempre\n"
            "**⏰ Tiempo límite para efectuar el pago:** 12 horas\n"
            "**💳 Opciones de pago:** Transferencia bancaria y establecimientos OXXO\n\n"
            "**📋 Para iniciar su trámite, utilice el comando /permiso**\n"
            "**⚠️ IMPORTANTE:** Su folio será eliminado automáticamente del sistema si no realiza el pago dentro del tiempo establecido",
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"[ERROR] Comando start: {e}")
        await message.answer("**❌ Error interno del sistema. Intente nuevamente en unos momentos.**", parse_mode="Markdown")

@dp.message(Command("permiso"))
async def permiso_cmd(message: types.Message, state: FSMContext):
    try:
        folios_activos = obtener_folios_usuario(message.from_user.id)
        mensaje_folios = ""
        if folios_activos:
            mensaje_folios = f"\n\n**📋 FOLIOS ACTIVOS:** {', '.join(folios_activos)}\n(Cada folio tiene su propio timer independiente de 12 horas)"
        await message.answer(
            "**🚗 SOLICITUD DE PERMISO DE CIRCULACIÓN - MORELOS**\n\n"
            "**📋 Inversión:** El costo es el mismo de siempre\n"
            "**⏰ Plazo para el pago:** 12 horas\n"
            "**💼 Concepto de pago:** Número de folio asignado\n\n"
            "Al proceder, usted acepta que el folio será eliminado si no efectúa el pago en el tiempo estipulado."
            + mensaje_folios + "\n\n"
            "Para comenzar, por favor indique la **MARCA** de su vehículo:",
            parse_mode="Markdown"
        )
        await state.set_state(PermisoForm.marca)
    except Exception as e:
        print(f"[ERROR] Comando permiso: {e}")
        await message.answer("**❌ ERROR INTERNO DEL SISTEMA**", parse_mode="Markdown")

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    try:
        marca = message.text.strip().upper()
        if not marca or len(marca) < 2:
            await message.answer("**⚠️ MARCA INVÁLIDA**\n\nEj.: NISSAN, TOYOTA, HONDA, VOLKSWAGEN\n\nIntente nuevamente:", parse_mode="Markdown")
            return
        await state.update_data(marca=marca)
        await message.answer(f"**✅ MARCA REGISTRADA:** {marca}\n\nAhora proporcione la **LÍNEA/MODELO**:", parse_mode="Markdown")
        await state.set_state(PermisoForm.linea)
    except Exception as e:
        print(f"[ERROR] get_marca: {e}")
        await state.clear()

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    try:
        linea = message.text.strip().upper()
        if not linea:
            await message.answer("**⚠️ LÍNEA/MODELO INVÁLIDO**\n\nEj.: SENTRA, TSURU, AVEO, JETTA", parse_mode="Markdown")
            return
        await state.update_data(linea=linea)
        await message.answer(f"**✅ LÍNEA CONFIRMADA:** {linea}\n\nAhora, indique el **AÑO** (ej. 2012):", parse_mode="Markdown")
        await state.set_state(PermisoForm.anio)
    except Exception as e:
        print(f"[ERROR] get_linea: {e}")
        await state.clear()

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    try:
        anio = message.text.strip()
        if not anio.isdigit() or not (1900 <= int(anio) <= datetime.now().year + 1):
            await message.answer("**⚠️ AÑO INVÁLIDO**\n\nIngresa un año numérico (ej. 2012).", parse_mode="Markdown")
            return
        await state.update_data(anio=anio)
        await message.answer(f"**✅ AÑO REGISTRADO:** {anio}\n\nAhora proporciona el **NÚMERO DE SERIE (VIN)**:", parse_mode="Markdown")
        await state.set_state(PermisoForm.serie)
    except Exception as e:
        print(f"[ERROR] get_anio: {e}")
        await state.clear()

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    try:
        serie = message.text.strip().upper().replace(" ", "")
        if len(serie) < 5:
            await message.answer("**⚠️ SERIE INVÁLIDA**\n\nMínimo 5 caracteres.", parse_mode="Markdown")
            return
        await state.update_data(serie=serie)
        await message.answer(f"**✅ SERIE CARGADA:** {serie}\n\nProporciona el **NÚMERO DE MOTOR**:", parse_mode="Markdown")
        await state.set_state(PermisoForm.motor)
    except Exception as e:
        print(f"[ERROR] get_serie: {e}")
        await state.clear()

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    try:
        motor = message.text.strip().upper()
        if len(motor) < 3:
            await message.answer("**⚠️ MOTOR INVÁLIDO**", parse_mode="Markdown")
            return
        await state.update_data(motor=motor)
        await message.answer(f"**✅ MOTOR REGISTRADO:** {motor}\n\nAhora el **COLOR** del vehículo:", parse_mode="Markdown")
        await state.set_state(PermisoForm.color)
    except Exception as e:
        print(f"[ERROR] get_motor: {e}")
        await state.clear()

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    try:
        color = message.text.strip().upper()
        if len(color) < 3:
            await message.answer("**⚠️ COLOR INVÁLIDO**", parse_mode="Markdown")
            return
        await state.update_data(color=color)
        await message.answer(f"**✅ COLOR REGISTRADO:** {color}\n\nIndica el **TIPO** (PARTICULAR / CARGA / PASAJEROS):", parse_mode="Markdown")
        await state.set_state(PermisoForm.tipo)
    except Exception as e:
        print(f"[ERROR] get_color: {e}")
        await state.clear()

@dp.message(PermisoForm.tipo)
async def get_tipo(message: types.Message, state: FSMContext):
    try:
        tipo = message.text.strip().upper()
        if len(tipo) < 3:
            await message.answer("**⚠️ TIPO INVÁLIDO**", parse_mode="Markdown")
            return
        await state.update_data(tipo=tipo)
        await message.answer(f"**✅ TIPO REGISTRADO:** {tipo}\n\nEscribe el **NOMBRE COMPLETO DEL CONTRIBUYENTE**:", parse_mode="Markdown")
        await state.set_state(PermisoForm.nombre)
    except Exception as e:
        print(f"[ERROR] get_tipo: {e}")
        await state.clear()

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    try:
        nombre = message.text.strip().upper()
        if len(nombre) < 5:
            await message.answer("**⚠️ NOMBRE INVÁLIDO**\n\nIngresa nombre y apellidos.", parse_mode="Markdown")
            return
        await state.update_data(nombre=nombre)

        folio, ok, err = generar_folio_automatico()
        if not ok:
            await message.answer(f"**❌ No se pudo generar el folio.** {err}", parse_mode="Markdown")
            await state.clear(); return

        placa = generar_placa_digital()

        # Fechas (CDMX/México) — impresión dd/mm/yyyy, BD ISO
        tz = ZoneInfo("America/Mexico_City")
        ahora = datetime.now(tz)
        vigencia_dias = 30
        vence = ahora + timedelta(days=vigencia_dias)

        fecha_iso = ahora.date().isoformat()
        fecha_ven_iso = vence.date().isoformat()
        fecha_texto = ahora.strftime("%d/%m/%Y")
        vigencia_texto = vence.strftime("%d/%m/%Y")

        data = await state.get_data()
        datos_pdf = {
            "folio": folio,
            "placa": placa,
            "fecha": fecha_texto,
            "vigencia": vigencia_texto,
            "marca": data["marca"],
            "linea": data["linea"],
            "anio": data["anio"],
            "serie": data["serie"],
            "motor": data["motor"],
            "color": data["color"],
            "tipo": data["tipo"],
            "nombre": nombre
        }

        ok_db, err_db = guardar_en_database(datos_pdf, fecha_iso, fecha_ven_iso, message.from_user.id, message.from_user.username or "")
        if not ok_db:
            await message.answer(f"**❌ Error guardando en base:** {err_db}", parse_mode="Markdown")
            await state.clear(); return

        fn_permiso, ok1, e1 = generar_pdf_principal(datos_pdf)
        fn_comp, ok2, e2 = generar_pdf_bueno(folio, data["serie"], nombre)
        if not ok1 or not ok2:
            await message.answer(f"**❌ Error generando PDFs**\n- Permiso: {e1}\n- Comprobante: {e2}", parse_mode="Markdown")
            await state.clear(); return

        await iniciar_timer_pago(message.from_user.id, folio)

        pending_comprobantes[folio] = {"user_id": message.from_user.id, "created_at": ahora.isoformat()}

        await message.answer(
            "**✅ SOLICITUD REGISTRADA**\n\n"
            f"**Folio:** {folio}\n"
            f"**Placa digital:** {placa}\n"
            f"**Contribuyente:** {nombre}\n"
            f"**Expedición:** {fecha_texto}\n"
            f"**Vigencia:** {vigencia_texto}\n"
            f"**Entidad:** MORELOS\n\n"
            "**💳 PAGO:** El costo es el mismo de siempre. Tienes **12 horas**.\n"
            "Envía tu **comprobante de pago (foto)** respondiendo con el **folio en el mensaje**.",
            parse_mode="Markdown"
        )
        try:
            await message.answer_document(FSInputFile(fn_comp), caption=f"Comprobante • Folio {folio}")
        except Exception as e:
            print(f"[WARN] Enviando comprobante: {e}")
        try:
            await message.answer_document(FSInputFile(fn_permiso), caption=f"Permiso (hojas) • Folio {folio}")
        except Exception as e:
            print(f"[WARN] Enviando permiso: {e}")

        await state.clear()
    except Exception as e:
        print(f"[ERROR] get_nombre: {e}")
        await message.answer("**❌ Error al cerrar la solicitud.** Intenta con **/permiso**.", parse_mode="Markdown")
        await state.clear()

# ========= RECEPCIÓN DE COMPROBANTES (FOTO) =========
@dp.message(lambda m: m.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    try:
        caption = (message.caption or "").upper()
        folio_detectado = ""
        for token in caption.replace("\n", " ").split():
            if token.isdigit() and token.startswith("345"):
                folio_detectado = token
                break
        if not folio_detectado:
            await message.reply("**⚠️ Incluye el FOLIO en el caption de la foto.** Ej.: `Comprobante folio 345123`", parse_mode="Markdown")
            return

        resp = supabase.table("folios_registrados").select("*").eq("folio", folio_detectado).execute()
        if not resp.data:
            await message.reply("**❌ Folio no encontrado.**", parse_mode="Markdown")
            return

        registro = resp.data[0]
        if registro.get("estado") == "PAGADO":
            await message.reply("**ℹ️ Ese folio ya está marcado como PAGADO.**", parse_mode="Markdown")
            return

        supabase.table("folios_registrados").update({"estado": "PAGADO"}).eq("folio", folio_detectado).execute()
        supabase.table("borradores_registros").update({"estado": "PAGADO"}).eq("folio", folio_detectado).execute()

        cancelar_timer_folio(folio_detectado)

        await message.reply(f"**✅ Comprobante recibido.** Folio **{folio_detectado}** marcado como **PAGADO**.", parse_mode="Markdown")
    except Exception as e:
        print(f"[ERROR] recibir_comprobante: {e}")
        await message.reply("**❌ Error procesando el comprobante.**", parse_mode="Markdown")

# ========= FASTAPI =========
app = FastAPI(title="Permisos Morelos")

@app.get("/healthz")
async def healthz():
    return {"ok": True, "service": "morelos-bot", "time": datetime.utcnow().isoformat()}

@app.get("/consulta/{folio}")
async def consulta_folio(folio: str, request: Request):
    try:
        res = supabase.table("folios_registrados").select(
            "folio, marca, linea, anio, numero_serie, numero_motor, color, nombre, fecha_expedicion, fecha_vencimiento, estado, entidad"
        ).eq("folio", folio).execute()

        if not res.data:
            return {"ok": False, "mensaje": "Folio no encontrado"}

        item = res.data[0]
        return {"ok": True, "data": item}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# === Webhook para Telegram (SIN polling) ===
@app.post(f"/webhook/{BOT_TOKEN}")
async def telegram_webhook(request: Request):
    data = await request.json()
    await dp.feed_update(bot, Update.model_validate(data))
    return {"ok": True}

# ====== LIFESPAN: configurar webhook al iniciar ======
@asynccontextmanager
async def lifespan(app: FastAPI):
    inicializar_folio_desde_supabase()
    # Limpia y setea webhook
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        print(f"[WARN] delete_webhook: {e}")

    webhook_url = f"{BASE_URL}/webhook/{BOT_TOKEN}"
    await bot.set_webhook(url=webhook_url)
    print(f"[WEBHOOK] Set to {webhook_url}")

    try:
        yield
    finally:
        try:
            await bot.delete_webhook(drop_pending_updates=False)
        except Exception as e:
            print(f"[WARN] delete_webhook on shutdown: {e}")

app.router.lifespan_context = lifespan

# ====== MAIN LOCAL ======
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
