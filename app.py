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
from aiogram.types import FSInputFile, ContentType
from contextlib import asynccontextmanager, suppress
import asyncio
import qrcode
from io import BytesIO
import random
import string
from PIL import Image

# CONFIG
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
URL_CONSULTA_BASE_MORELOS = "https://morelosgobmovilidad-y-transporte.onrender.com"
OUTPUT_DIR = "documentos"
PLANTILLA_PDF = "morelos_hoja1_imagen.pdf"
PLANTILLA_BUENO = "morelosvergas1.pdf"

# ADMIN USER ID - Cambia este ID por el tuyo
ADMIN_USER_ID = 123456789  # REEMPLAZA CON TU USER_ID REAL

# Coordenadas Morelos
coords_morelos = {
    "folio": (665,282,18,(1,0,0)),
    "placa": (200,200,60,(0,0,0)),
    "fecha": (200,340,14,(0,0,0)),
    "vigencia": (600,340,14,(0,0,0)),
    "marca": (110,425,14,(0,0,0)),
    "serie": (460,420,14,(0,0,0)),
    "linea": (110,455,14,(0,0,0)),
    "motor": (460,445,14,(0,0,0)),
    "anio": (110,485,14,(0,0,0)),
    "color": (460,395,14,(0,0,0)),
    "tipo": (510,470,14,(0,0,0)),
    "nombre": (150,370,14,(0,0,0)),
    "fecha_hoja2": (126,310,15,(0,0,0)),
}

os.makedirs(OUTPUT_DIR, exist_ok=True)

# SUPABASE
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# BOT
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# TIMER MANAGEMENT
timers_activos = {}
user_folios = {}
pending_comprobantes = {}
folios_protegidos = set()

# QR DINÁMICO
def generar_qr_dinamico_morelos(folio):
    try:
        url_directa = f"{URL_CONSULTA_BASE_MORELOS}/consulta/{folio}"
        qr = qrcode.QRCode(version=2, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=4, border=1)
        qr.add_data(url_directa)
        qr.make(fit=True)
        img_qr = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        return img_qr, url_directa
    except Exception as e:
        print(f"[ERROR QR] {e}")
        return None, None

# TIMER FUNCTIONS
async def eliminar_folio_automatico(folio: str):
    try:
        if folio in folios_protegidos:
            print(f"[ADMIN PROTECTION] Folio {folio} protegido")
            limpiar_timer_folio(folio)
            return
        
        user_id = timers_activos.get(folio, {}).get("user_id")
        
        supabase.table("folios_registrados").delete().eq("folio", folio).execute()
        supabase.table("borradores_registros").delete().eq("folio", folio).execute()
        
        if user_id:
            await bot.send_message(
                user_id,
                f"🔔 **NOTIFICACIÓN IMPORTANTE**\n\n"
                f"Su folio **{folio}** ha sido eliminado del sistema por vencimiento del plazo de pago.\n\n"
                f"Para tramitar un nuevo permiso utilize /permiso",
                parse_mode="Markdown"
            )
        
        limpiar_timer_folio(folio)
    except Exception as e:
        print(f"Error eliminando folio {folio}: {e}")

async def enviar_recordatorio(folio: str, minutos_restantes: int):
    try:
        if folio not in timers_activos or folio in folios_protegidos:
            return
            
        user_id = timers_activos[folio]["user_id"]
        await bot.send_message(
            user_id,
            f"🔔 **RECORDATORIO DE PAGO**\n\n"
            f"**Folio:** {folio}\n"
            f"**Tiempo restante:** {minutos_restantes} minutos\n\n"
            f"Favor de enviar su comprobante de pago adjuntando una imagen.",
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"Error enviando recordatorio: {e}")

async def iniciar_timer_pago(user_id: int, folio: str):
    async def timer_task():
        print(f"[TIMER] Iniciado para folio {folio}")
        
        # Recordatorios cada 2 horas
        for horas in [2, 4, 6, 8, 10]:
            await asyncio.sleep(2 * 60 * 60)
            if folio not in timers_activos or folio in folios_protegidos:
                return
            await enviar_recordatorio(folio, (12 - horas) * 60)
        
        # Recordatorio final a 30 minutos
        await asyncio.sleep(1.5 * 60 * 60)
        if folio in timers_activos and folio not in folios_protegidos:
            await enviar_recordatorio(folio, 30)
        
        # Eliminación final
        await asyncio.sleep(30 * 60)
        if folio in timers_activos:
            await eliminar_folio_automatico(folio)

    task = asyncio.create_task(timer_task())
    timers_activos[folio] = {"task": task, "user_id": user_id, "start_time": datetime.now()}
    
    if user_id not in user_folios:
        user_folios[user_id] = []
    user_folios[user_id].append(folio)

def cancelar_timer_folio(folio: str):
    if folio in timers_activos:
        timers_activos[folio]["task"].cancel()
        user_id = timers_activos[folio]["user_id"]
        del timers_activos[folio]
        
        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]:
                del user_folios[user_id]

def proteger_folio_admin(folio: str):
    folios_protegidos.add(folio)
    if folio in timers_activos:
        timers_activos[folio]["task"].cancel()
        user_id = timers_activos[folio]["user_id"]
        del timers_activos[folio]
        return user_id
    return None

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

# FOLIO SYSTEM
folio_counter = {"count": 1}

def inicializar_folio_desde_supabase():
    try:
        response = supabase.table("folios_registrados") \
            .select("folio") \
            .eq("entidad", "morelos") \
            .order("folio", desc=True) \
            .execute()

        ultimo_numero = 0
        if response.data:
            for registro in response.data:
                folio = registro["folio"]
                if folio.startswith("456"):
                    try:
                        numero = int(folio[3:])
                        if numero > ultimo_numero:
                            ultimo_numero = numero
                    except ValueError:
                        continue
        
        folio_counter["count"] = ultimo_numero + 1
        print(f"[INFO] Contador inicializado en: 456{folio_counter['count']}")
        
    except Exception as e:
        print(f"[ERROR] Inicializando folio: {e}")
        folio_counter["count"] = 1

def generar_folio_automatico() -> tuple:
    for intento in range(50):
        folio = f"456{folio_counter['count']}"
        
        try:
            response = supabase.table("folios_registrados").select("folio").eq("folio", folio).execute()
            
            if not response.data:
                folio_counter["count"] += 1
                return folio, True, ""
            
            folio_counter["count"] += 1
            
        except Exception as e:
            if intento >= 45:
                folio_final = f"456{folio_counter['count']}"
                folio_counter["count"] += 1
                return folio_final, True, ""
            folio_counter["count"] += 1
            continue
    
    # Fallback
    import time
    timestamp = int(time.time()) % 1000000
    return f"456{timestamp}", True, ""

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
                    l2 = abc[i2+1]
                    l3 = "A"
                else:
                    l1 = abc[(abc.index(l1)+1)%26]
                    l2 = l3 = "A"
            nuevo = f"{l1}{l2}{l3}0000"
        
        with open(archivo, "a") as f:
            f.write(nuevo+"\n")
        
        return nuevo
    except Exception as e:
        letras = ''.join(random.choices(abc, k=3))
        numeros = ''.join(random.choices('0123456789', k=4))
        return f"{letras}{numeros}"

# FSM STATES
class PermisoForm(StatesGroup):
    marca = State()
    linea = State()
    anio = State()
    serie = State()
    motor = State()
    color = State()
    tipo = State()
    nombre = State()

# PDF FUNCTIONS
def generar_pdf_principal(datos: dict) -> tuple:
    try:
        doc = fitz.open(PLANTILLA_PDF)
        pg = doc[0]
        
        # Insertar datos
        pg.insert_text(coords_morelos["folio"][:2], datos["folio"], fontsize=coords_morelos["folio"][2], color=coords_morelos["folio"][3])
        pg.insert_text(coords_morelos["placa"][:2], datos["placa"], fontsize=coords_morelos["placa"][2], color=coords_morelos["placa"][3])
        pg.insert_text(coords_morelos["fecha"][:2], datos["fecha"], fontsize=coords_morelos["fecha"][2], color=coords_morelos["fecha"][3])
        pg.insert_text(coords_morelos["vigencia"][:2], datos["vigencia"], fontsize=coords_morelos["vigencia"][2], color=coords_morelos["vigencia"][3])
        pg.insert_text(coords_morelos["marca"][:2], datos["marca"], fontsize=coords_morelos["marca"][2], color=coords_morelos["marca"][3])
        pg.insert_text(coords_morelos["serie"][:2], datos["serie"], fontsize=coords_morelos["serie"][2], color=coords_morelos["serie"][3])
        pg.insert_text(coords_morelos["linea"][:2], datos["linea"], fontsize=coords_morelos["linea"][2], color=coords_morelos["linea"][3])
        pg.insert_text(coords_morelos["motor"][:2], datos["motor"], fontsize=coords_morelos["motor"][2], color=coords_morelos["motor"][3])
        pg.insert_text(coords_morelos["anio"][:2], datos["anio"], fontsize=coords_morelos["anio"][2], color=coords_morelos["anio"][3])
        pg.insert_text(coords_morelos["color"][:2], datos["color"], fontsize=coords_morelos["color"][2], color=coords_morelos["color"][3])
        pg.insert_text(coords_morelos["tipo"][:2], datos["tipo"], fontsize=coords_morelos["tipo"][2], color=coords_morelos["tipo"][3])
        pg.insert_text(coords_morelos["nombre"][:2], datos["nombre"], fontsize=coords_morelos["nombre"][2], color=coords_morelos["nombre"][3])
        
        # QR DINÁMICO
        qr_x, qr_y, qr_width, qr_height = 595, 148, 115, 115
        
        img_qr, url_qr = generar_qr_dinamico_morelos(datos["folio"])
        if img_qr:
            buf = BytesIO()
            img_qr.save(buf, format="PNG")
            buf.seek(0)
            qr_pix = fitz.Pixmap(buf.read())
            rect_qr = fitz.Rect(qr_x, qr_y, qr_x + qr_width, qr_y + qr_height)
            pg.insert_image(rect_qr, pixmap=qr_pix, overlay=True)
        
        # Hoja 2 si existe
        if len(doc) > 1:
            pg2 = doc[1]
            pg2.insert_text(coords_morelos["fecha_hoja2"][:2], datos["vigencia"], 
                          fontsize=coords_morelos["fecha_hoja2"][2], color=coords_morelos["fecha_hoja2"][3])
        
        filename = f"{OUTPUT_DIR}/{datos['folio']}_morelos.pdf"
        doc.save(filename)
        doc.close()
        return filename, True, ""
    except Exception as e:
        return "", False, str(e)

def generar_pdf_bueno(folio: str, numero_serie: str, nombre: str) -> tuple:
    try:
        doc = fitz.open(PLANTILLA_BUENO)
        page = doc[0]
        
        ahora = datetime.now(ZoneInfo("America/Mexico_City"))
        
        page.insert_text((155, 245), nombre.upper(), fontsize=18, fontname="helv")
        page.insert_text((1045, 205), folio, fontsize=20, fontname="helv")
        page.insert_text((1045, 275), ahora.strftime("%d/%m/%Y"), fontsize=20, fontname="helv")
        page.insert_text((1045, 348), ahora.strftime("%H:%M:%S"), fontsize=20, fontname="helv")
        
        filename = f"{OUTPUT_DIR}/{folio}.pdf"
        doc.save(filename)
        doc.close()
        return filename, True, ""
    except Exception as e:
        return "", False, str(e)

def guardar_en_database(datos: dict, fecha_iso: str, fecha_ven_iso: str, user_id: int, username: str) -> tuple:
    try:
        supabase.table("folios_registrados").insert({
            "folio": datos["folio"], "marca": datos["marca"], "linea": datos["linea"],
            "anio": datos["anio"], "numero_serie": datos["serie"], "numero_motor": datos["motor"],
            "color": datos["color"], "nombre": datos["nombre"],
            "fecha_expedicion": fecha_iso, "fecha_vencimiento": fecha_ven_iso,
            "entidad": "morelos", "estado": "PENDIENTE", "user_id": user_id,
            "username": username or "Sin username"
        }).execute()
        
        supabase.table("borradores_registros").insert({
            "folio": datos["folio"], "entidad": "Morelos", "numero_serie": datos["serie"],
            "marca": datos["marca"], "linea": datos["linea"], "numero_motor": datos["motor"],
            "anio": datos["anio"], "color": datos["color"],
            "fecha_expedicion": fecha_iso, "fecha_vencimiento": fecha_ven_iso,
            "contribuyente": datos["nombre"], "estado": "PENDIENTE", "user_id": user_id
        }).execute()
        
        return True, ""
    except Exception as e:
        return False, str(e)

# HANDLERS
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🏛️ **Sistema Digital de Permisos del Estado de Morelos**\n\n"
        "Bienvenido a la plataforma oficial para la gestión de permisos de circulación vehicular.\n\n"
        "**📋 Información del servicio:**\n"
        "• Costo: Consultar con el operador\n"
        "• Plazo de pago: 12 horas\n"
        "• Métodos de pago: Transferencia bancaria y establecimientos OXXO\n\n"
        "**Para iniciar su trámite, utilice el comando /permiso**\n\n"
        "⚠️ Importante: Los folios no pagados dentro del plazo establecido serán eliminados automáticamente.",
        parse_mode="Markdown"
    )

@dp.message(Command("permiso"))
async def permiso_cmd(message: types.Message, state: FSMContext):
    folios_activos = obtener_folios_usuario(message.from_user.id)
    info_folios = f"\n\n📋 **Folios activos:** {', '.join(folios_activos)}" if folios_activos else ""
    
    await message.answer(
        f"🚗 **Solicitud de Permiso de Circulación - Morelos**\n\n"
        f"Iniciamos el proceso de registro de su vehículo. La información proporcionada será utilizada para generar su permiso oficial.{info_folios}\n\n"
        f"**Paso 1 de 8:** Proporcione la marca de su vehículo (ej: NISSAN, TOYOTA, FORD):",
        parse_mode="Markdown"
    )
    await state.set_state(PermisoForm.marca)

# COMANDO ADMIN SERO
@dp.message(lambda m: m.text and m.text.upper().startswith("SERO") and len(m.text) > 4)
async def comando_admin_sero(message: types.Message):
    if message.from_user.id != ADMIN_USER_ID:
        return
    
    texto = message.text.upper()
    folio = texto[4:].strip()
    
    if not folio.startswith("456") or not folio[3:].isdigit():
        await message.reply("❌ Formato incorrecto. Use: SERO456XXXXX", parse_mode="Markdown")
        return
    
    resp = supabase.table("folios_registrados").select("*").eq("folio", folio).execute()
    if not resp.data:
        await message.reply(f"❌ Folio {folio} no encontrado.", parse_mode="Markdown")
        return
    
    user_id_cliente = proteger_folio_admin(folio)
    
    await message.reply(
        f"✅ **FOLIO PROTEGIDO**\n\n"
        f"El folio {folio} ha sido protegido y no será eliminado automáticamente.",
        parse_mode="Markdown"
    )
    
    if user_id_cliente:
        try:
            await bot.send_message(
                user_id_cliente,
                f"🛡️ Su folio {folio} ha sido protegido por el administrador.\n"
                f"Ya no será eliminado automáticamente.",
                parse_mode="Markdown"
            )
        except:
            pass

# HANDLERS DEL FSM
@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    marca = message.text.strip().upper()
    if len(marca) < 2:
        await message.answer("⚠️ Ingrese una marca válida (mínimo 2 caracteres).")
        return
    
    await state.update_data(marca=marca)
    await message.answer(f"✅ Marca registrada: **{marca}**\n\n**Paso 2 de 8:** Proporcione el modelo o línea del vehículo:", parse_mode="Markdown")
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    linea = message.text.strip().upper()
    if len(linea) < 1:
        await message.answer("⚠️ Ingrese un modelo válido.")
        return
    
    await state.update_data(linea=linea)
    await message.answer(f"✅ Modelo registrado: **{linea}**\n\n**Paso 3 de 8:** Indique el año del vehículo:", parse_mode="Markdown")
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or not (1900 <= int(anio) <= datetime.now().year + 1):
        await message.answer("⚠️ Ingrese un año válido (ej: 2012).")
        return
    
    await state.update_data(anio=anio)
    await message.answer(f"✅ Año registrado: **{anio}**\n\n**Paso 4 de 8:** Proporcione el número de serie (VIN):", parse_mode="Markdown")
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    serie = message.text.strip().upper().replace(" ", "")
    if len(serie) < 5:
        await message.answer("⚠️ Ingrese un número de serie válido (mínimo 5 caracteres).")
        return
    
    await state.update_data(serie=serie)
    await message.answer(f"✅ Serie registrada: **{serie}**\n\n**Paso 5 de 8:** Proporcione el número de motor:", parse_mode="Markdown")
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    motor = message.text.strip().upper()
    if len(motor) < 3:
        await message.answer("⚠️ Ingrese un número de motor válido.")
        return
    
    await state.update_data(motor=motor)
    await message.answer(f"✅ Motor registrado: **{motor}**\n\n**Paso 6 de 8:** Indique el color del vehículo:", parse_mode="Markdown")
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    color = message.text.strip().upper()
    if len(color) < 3:
        await message.answer("⚠️ Ingrese un color válido.")
        return
    
    await state.update_data(color=color)
    await message.answer(f"✅ Color registrado: **{color}**\n\n**Paso 7 de 8:** Indique el tipo de vehículo (PARTICULAR/CARGA/PASAJEROS):", parse_mode="Markdown")
    await state.set_state(PermisoForm.tipo)

@dp.message(PermisoForm.tipo)
async def get_tipo(message: types.Message, state: FSMContext):
    tipo = message.text.strip().upper()
    if len(tipo) < 3:
        await message.answer("⚠️ Ingrese un tipo válido (PARTICULAR/CARGA/PASAJEROS).")
        return
    
    await state.update_data(tipo=tipo)
    await message.answer(f"✅ Tipo registrado: **{tipo}**\n\n**Paso 8 de 8:** Proporcione el nombre completo del contribuyente:", parse_mode="Markdown")
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    nombre = message.text.strip().upper()
    if len(nombre) < 5:
        await message.answer("⚠️ Ingrese el nombre completo (nombre y apellidos).")
        return
    
    await state.update_data(nombre=nombre)
    
    # Procesar solicitud
    folio, ok, err = generar_folio_automatico()
    if not ok:
        await message.answer(f"❌ Error generando folio: {err}")
        await state.clear()
        return
    
    placa = generar_placa_digital()
    
    # Fechas
    tz = ZoneInfo("America/Mexico_City")
    ahora = datetime.now(tz)
    vence = ahora + timedelta(days=30)
    
    fecha_iso = ahora.strftime("%Y-%m-%d")
    fecha_ven_iso = vence.strftime("%Y-%m-%d")
    fecha_texto = ahora.strftime("%d/%m/%Y")
    vigencia_texto = vence.strftime("%d/%m/%Y")
    
    data = await state.get_data()
    datos_pdf = {
        "folio": folio, "placa": placa, "fecha": fecha_texto, "vigencia": vigencia_texto,
        "marca": data["marca"], "linea": data["linea"], "anio": data["anio"],
        "serie": data["serie"], "motor": data["motor"], "color": data["color"],
        "tipo": data["tipo"], "nombre": nombre
    }
    
    # Guardar en BD
    ok_db, err_db = guardar_en_database(datos_pdf, fecha_iso, fecha_ven_iso, 
                                       message.from_user.id, message.from_user.username or "")
    if not ok_db:
        await message.answer(f"❌ Error guardando datos: {err_db}")
        await state.clear()
        return
    
    # Generar PDFs
    await message.answer("⏳ Generando documentos, por favor espere...")
    
    fn_permiso, ok1, e1 = generar_pdf_principal(datos_pdf)
    fn_comp, ok2, e2 = generar_pdf_bueno(folio, data["serie"], nombre)
    
    if not ok1 or not ok2:
        await message.answer(f"❌ Error generando documentos\nPermiso: {e1}\nComprobante: {e2}")
        await state.clear()
        return
    
    # Iniciar timer
    await iniciar_timer_pago(message.from_user.id, folio)
    
    pending_comprobantes[folio] = {
        "user_id": message.from_user.id,
        "created_at": ahora.isoformat()
    }
    
    # Enviar resumen
    await message.answer(
        f"✅ **REGISTRO COMPLETADO EXITOSAMENTE**\n\n"
        f"**Información del permiso:**\n"
        f"• Folio: **{folio}**\n"
        f"• Placa digital: **{placa}**\n"
        f"• Contribuyente: **{nombre}**\n"
        f"• Fecha de expedición: **{fecha_texto}**\n"
        f"• Vigencia: **{vigencia_texto}**\n"
        f"• Entidad: **MORELOS**\n\n"
        f"⏰ **Plazo de pago: 12 horas**\n"
        f"📸 **Para completar el trámite, envíe su comprobante de pago incluyendo el folio en el mensaje.**\n\n"
        f"📄 **A continuación se envían sus documentos:**",
        parse_mode="Markdown"
    )
    
    # Enviar documentos
    try:
        await message.answer_document(FSInputFile(fn_comp), caption=f"📋 Comprobante de solicitud • Folio: {folio}")
    except Exception as e:
        print(f"[WARN] Error enviando comprobante: {e}")
    
    try:
        await message.answer_document(FSInputFile(fn_permiso), caption=f"🎫 Permiso de circulación • Folio: {folio}")
    except Exception as e:
        print(f"[WARN] Error enviando permiso: {e}")
    
    # Mensaje final con opción de nuevo trámite
    await message.answer(
        f"🎉 **¡PROCESO COMPLETADO!**\n\n"
        f"Sus documentos han sido generados y enviados correctamente.\n"
        f"Recuerde enviar su comprobante de pago dentro del plazo establecido.\n\n"
        f"**📋 Para realizar otro trámite use /permiso**",
        parse_mode="Markdown"
    )
    
    await state.clear()

# RECEPCIÓN DE COMPROBANTES
@dp.message(lambda m: m.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    try:
        caption = (message.caption or "").upper()
        folio_detectado = ""
        
        # Buscar folio en el caption
        for token in caption.replace("\n", " ").split():
            if token.startswith("456") and token[3:].isdigit():
                folio_detectado = token
                break
        
        if not folio_detectado:
            await message.reply(
                "⚠️ **Información requerida**\n\n"
                "Para procesar su comprobante, incluya el número de folio en el mensaje de la imagen.\n"
                "Ejemplo: `Comprobante folio 4561234`",
                parse_mode="Markdown"
            )
            return
        
        # Validar folio
        resp = supabase.table("folios_registrados").select("*").eq("folio", folio_detectado).execute()
        if not resp.data:
            await message.reply(
                f"❌ **Folio no encontrado**\n\n"
                f"El folio {folio_detectado} no existe en el sistema. Verifique el número.",
                parse_mode="Markdown"
            )
            return
        
        registro = resp.data[0]
        if registro.get("estado") == "PAGADO":
            await message.reply(
                f"ℹ️ **Estado del folio**\n\n"
                f"El folio {folio_detectado} ya se encuentra marcado como pagado.",
                parse_mode="Markdown"
            )
            return
        
        # Actualizar estado
        supabase.table("folios_registrados").update({"estado": "PAGADO"}).eq("folio", folio_detectado).execute()
        supabase.table("borradores_registros").update({"estado": "PAGADO"}).eq("folio", folio_detectado).execute()
        
        # Cancelar timer
        cancelar_timer_folio(folio_detectado)
        
        await message.reply(
            f"✅ **COMPROBANTE RECIBIDO**\n\n"
            f"Su comprobante de pago ha sido recibido y validado correctamente.\n"
            f"**Folio {folio_detectado}** actualizado a estado: **PAGADO**\n\n"
            f"Gracias por utilizar nuestro servicio.\n\n"
            f"**📋 Para realizar otro trámite use /permiso**",
            parse_mode="Markdown"
        )
        
    except Exception as e:
        print(f"[ERROR] recibir_comprobante: {e}")
        await message.reply(
            "❌ **Error del sistema**\n\n"
            "Ocurrió un error procesando su comprobante. Por favor, intente nuevamente.",
            parse_mode="Markdown"
        )

# FASTAPI ROUTES
app = FastAPI(title="Sistema de Permisos Morelos", description="API para consulta de permisos")

@app.get("/healthz")
async def health_check():
    return {
        "status": "ok", 
        "service": "morelos-bot", 
        "timestamp": datetime.utcnow().isoformat(),
        "version": "2.0"
    }

@app.get("/consulta/{folio}")
async def consulta_folio(folio: str, request: Request):
    """Endpoint para consultar información de un folio"""
    try:
        response = supabase.table("folios_registrados").select(
            "folio, marca, linea, anio, numero_serie, numero_motor, color, nombre, "
            "fecha_expedicion, fecha_vencimiento, estado, entidad"
        ).eq("folio", folio).execute()
        
        if not response.data:
            return {
                "success": False, 
                "message": "Folio no encontrado en el sistema",
                "folio": folio
            }
        
        registro = response.data[0]
        return {
            "success": True,
            "message": "Folio encontrado",
            "data": {
                "folio": registro["folio"],
                "contribuyente": registro["nombre"],
                "vehiculo": {
                    "marca": registro["marca"],
                    "modelo": registro["linea"],
                    "anio": registro["anio"],
                    "serie": registro["numero_serie"],
                    "motor": registro["numero_motor"],
                    "color": registro["color"]
                },
                "fechas": {
                    "expedicion": registro["fecha_expedicion"],
                    "vencimiento": registro["fecha_vencimiento"]
                },
                "estado": registro["estado"],
                "entidad": registro["entidad"]
            }
        }
        
    except Exception as e:
        print(f"[ERROR] consulta_folio: {e}")
        return {
            "success": False,
            "message": "Error interno del servidor",
            "error": str(e)
        }

@app.get("/")
async def root():
    return {
        "message": "Sistema de Permisos del Estado de Morelos",
        "version": "2.0",
        "endpoints": {
            "health": "/healthz",
            "consulta": "/consulta/{folio}"
        }
    }

# LIFESPAN MANAGEMENT
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gestión del ciclo de vida de la aplicación"""
    print("[STARTUP] Iniciando sistema...")
    
    # Inicializar contador de folios
    inicializar_folio_desde_supabase()
    
    # Limpiar webhook previo
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        print("[STARTUP] Webhook limpiado")
    except Exception as e:
        print(f"[WARN] Error limpiando webhook: {e}")
    
    # Iniciar polling
    from aiogram.enums import UpdateType
    allowed_updates = [u.value for u in UpdateType]
    
    polling_task = asyncio.create_task(
        dp.start_polling(bot, allowed_updates=allowed_updates)
    )
    
    print("[STARTUP] Bot iniciado con polling")
    print(f"[INFO] Admin ID configurado: {ADMIN_USER_ID}")
    
    try:
        yield
    finally:
        print("[SHUTDOWN] Deteniendo sistema...")
        polling_task.cancel()
        with suppress(asyncio.CancelledError):
            await polling_task
        print("[SHUTDOWN] Sistema detenido")

app.router.lifespan_context = lifespan

# MAIN ENTRY POINT
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    print(f"[MAIN] Iniciando servidor en puerto {port}")
    uvicorn.run(
        app, 
        host="0.0.0.0", 
        port=port,
        log_level="info"
    )
