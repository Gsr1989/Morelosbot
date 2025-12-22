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
from aiogram.types import FSInputFile, ContentType, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
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

PRECIO_PERMISO = 200

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
    "qr_hoja1": (400,500,70,70)
}

os.makedirs(OUTPUT_DIR, exist_ok=True)

# SUPABASE
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# BOT
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# TIMER MANAGEMENT - 36 HORAS
timers_activos = {}
user_folios = {}
pending_comprobantes = {}

TOTAL_MINUTOS_TIMER = 36 * 60

def generar_qr_dinamico_morelos(folio):
    try:
        url_directa = f"{URL_CONSULTA_BASE_MORELOS}/consulta/{folio}"
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
                f"⏰ TIEMPO AGOTADO - MORELOS\n\n"
                f"El folio {folio} ha sido eliminado del sistema por no completar el pago en 36 horas.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
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
            f"⚡ RECORDATORIO DE PAGO - MORELOS\n\n"
            f"Folio: {folio}\n"
            f"Tiempo restante: {minutos_restantes} minutos\n"
            f"Monto: ${PRECIO_PERMISO}\n\n"
            f"📸 Envíe su comprobante de pago (imagen) para validar el trámite.\n\n"
            f"📋 Para generar otro permiso use /chuleta"
        )
    except Exception as e:
        print(f"Error enviando recordatorio para folio {folio}: {e}")

async def iniciar_timer_eliminacion(user_id: int, folio: str):
    async def timer_task():
        print(f"[TIMER] Iniciado para folio {folio}, usuario {user_id} (36 horas)")
        
        await asyncio.sleep(34.5 * 3600)

        if folio not in timers_activos: return
        await enviar_recordatorio(folio, 90)
        await asyncio.sleep(30 * 60)

        if folio not in timers_activos: return
        await enviar_recordatorio(folio, 60)
        await asyncio.sleep(30 * 60)

        if folio not in timers_activos: return
        await enviar_recordatorio(folio, 30)
        await asyncio.sleep(20 * 60)

        if folio not in timers_activos: return
        await enviar_recordatorio(folio, 10)
        await asyncio.sleep(10 * 60)

        if folio in timers_activos:
            print(f"[TIMER] Expirado para folio {folio} - eliminando")
            await eliminar_folio_automatico(folio)
    
    task = asyncio.create_task(timer_task())
    timers_activos[folio] = {
        "task": task,
        "user_id": user_id,
        "start_time": datetime.now()
    }
    
    if user_id not in user_folios:
        user_folios[user_id] = []
    user_folios[user_id].append(folio)
    
    print(f"[SISTEMA] Timer 36h iniciado para folio {folio}, total timers: {len(timers_activos)}")

def cancelar_timer_folio(folio: str):
    if folio in timers_activos:
        timers_activos[folio]["task"].cancel()
        user_id = timers_activos[folio]["user_id"]
        
        del timers_activos[folio]
        
        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]:
                del user_folios[user_id]
        
        print(f"[SISTEMA] Timer cancelado para folio {folio}")

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

# FOLIO SYSTEM CON PREFIJO 456
folio_counter = {"count": 1}
MAX_INTENTOS_FOLIO = 100000

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
        print(f"[INFO] Folio Morelos inicializado: último 456{ultimo_numero}, siguiente: 456{folio_counter['count']}")
        
    except Exception as e:
        print(f"[ERROR] Al inicializar folio Morelos: {e}")
        folio_counter["count"] = 1

def generar_folio_automatico() -> tuple:
    for intento in range(MAX_INTENTOS_FOLIO):
        folio = f"456{folio_counter['count']}"
        
        try:
            response = supabase.table("folios_registrados") \
                .select("folio") \
                .eq("folio", folio) \
                .execute()
            
            if response.data and len(response.data) > 0:
                folio_counter["count"] += 1
                continue
            
            folio_counter["count"] += 1
            return folio, True, ""
            
        except Exception as e:
            print(f"[ERROR] Verificando folio {folio}: {e}")
            if intento >= 45:
                folio_final = f"456{folio_counter['count']}"
                folio_counter["count"] += 1
                return folio_final, True, ""
            
            folio_counter["count"] += 1
            continue
    
    import time
    timestamp = int(time.time()) % 1000000
    folio_timestamp = f"456{timestamp}"
    return folio_timestamp, True, ""
    
def generar_placa_digital():
    archivo = "placas_digitales.txt"
    abc = string.ascii_uppercase
    try:
        if not os.path.exists(archivo):
            with open(archivo, "w") as f:
                f.write("GZR1999\n")
        
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
        print(f"[ERROR] Generando placa digital: {e}")
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

# ============ GENERACIÓN PDF UNIFICADO (2 PÁGINAS EN 1 ARCHIVO) ============
def generar_pdf_unificado(datos: dict) -> tuple:
    """Genera UN SOLO PDF con ambas plantillas (2 páginas)"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filename = f"{OUTPUT_DIR}/{datos['folio']}_completo.pdf"
    
    try:
        # ===== PÁGINA 1: PLANTILLA PRINCIPAL =====
        doc1 = fitz.open(PLANTILLA_PDF)
        pg1 = doc1[0]
        
        pg1.insert_text(coords_morelos["folio"][:2], datos["folio"], fontsize=coords_morelos["folio"][2], color=coords_morelos["folio"][3])
        pg1.insert_text(coords_morelos["placa"][:2], datos["placa"], fontsize=coords_morelos["placa"][2], color=coords_morelos["placa"][3])
        pg1.insert_text(coords_morelos["fecha"][:2], datos["fecha"], fontsize=coords_morelos["fecha"][2], color=coords_morelos["fecha"][3])
        pg1.insert_text(coords_morelos["vigencia"][:2], datos["vigencia"], fontsize=coords_morelos["vigencia"][2], color=coords_morelos["vigencia"][3])
        pg1.insert_text(coords_morelos["marca"][:2], datos["marca"], fontsize=coords_morelos["marca"][2], color=coords_morelos["marca"][3])
        pg1.insert_text(coords_morelos["serie"][:2], datos["serie"], fontsize=coords_morelos["serie"][2], color=coords_morelos["serie"][3])
        pg1.insert_text(coords_morelos["linea"][:2], datos["linea"], fontsize=coords_morelos["linea"][2], color=coords_morelos["linea"][3])
        pg1.insert_text(coords_morelos["motor"][:2], datos["motor"], fontsize=coords_morelos["motor"][2], color=coords_morelos["motor"][3])
        pg1.insert_text(coords_morelos["anio"][:2], datos["anio"], fontsize=coords_morelos["anio"][2], color=coords_morelos["anio"][3])
        pg1.insert_text(coords_morelos["color"][:2], datos["color"], fontsize=coords_morelos["color"][2], color=coords_morelos["color"][3])
        pg1.insert_text(coords_morelos["tipo"][:2], datos["tipo"], fontsize=coords_morelos["tipo"][2], color=coords_morelos["tipo"][3])
        pg1.insert_text(coords_morelos["nombre"][:2], datos["nombre"], fontsize=coords_morelos["nombre"][2], color=coords_morelos["nombre"][3])
        
        # QR dinámico en página 1
        qr_x = 595
        qr_y = 148
        qr_width = 115
        qr_height = 115

        img_qr, url_qr = generar_qr_dinamico_morelos(datos["folio"])
        if img_qr:
            buf = BytesIO()
            img_qr.save(buf, format="PNG")
            buf.seek(0)
            qr_pix = fitz.Pixmap(buf.read())
            
            rect_qr = fitz.Rect(qr_x, qr_y, qr_x + qr_width, qr_y + qr_height)
            pg1.insert_image(rect_qr, pixmap=qr_pix, overlay=True)
            print(f"[QR MORELOS] Insertado en página 1")
        
        if len(doc1) > 1:
            pg2 = doc1[1]
            pg2.insert_text(
                coords_morelos["fecha_hoja2"][:2],
                datos["vigencia"],
                fontsize=coords_morelos["fecha_hoja2"][2],
                color=coords_morelos["fecha_hoja2"][3]
            )
        
        # ===== PÁGINA 2: PLANTILLA SIMPLE =====
        doc2 = fitz.open(PLANTILLA_BUENO)
        page2 = doc2[0]
        
        ahora = datetime.now(ZoneInfo("America/Mexico_City"))
        
        page2.insert_text((155, 245), datos["nombre"].upper(), fontsize=18, fontname="helv")
        page2.insert_text((1045, 205), datos["folio"], fontsize=20, fontname="helv")
        page2.insert_text((1045, 275), ahora.strftime("%d/%m/%Y"), fontsize=20, fontname="helv")
        page2.insert_text((1045, 348), ahora.strftime("%H:%M:%S"), fontsize=20, fontname="helv")
        
        # ===== UNIR AMBAS PÁGINAS =====
        doc1.insert_pdf(doc2)
        doc2.close()
        
        doc1.save(filename)
        doc1.close()
        
        print(f"[PDF UNIFICADO MORELOS] ✅ Generado: {filename}")
        return filename, True, ""
        
    except Exception as e:
        error_msg = f"Error generando PDF unificado: {str(e)}"
        print(f"[ERROR PDF] {error_msg}")
        return "", False, error_msg

# HANDLERS
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🏛️ SISTEMA DIGITAL DEL ESTADO DE MORELOS\n\n"
        f"💰 Costo: ${PRECIO_PERMISO}\n"
        "⏰ Tiempo límite: 36 horas\n\n"
        "⚠️ IMPORTANTE: Su folio será eliminado automáticamente si no realiza el pago dentro del tiempo límite"
    )

@dp.message(Command("chuleta"))
async def chuleta_cmd(message: types.Message, state: FSMContext):
    folios_activos = obtener_folios_usuario(message.from_user.id)
    mensaje_folios = ""
    if folios_activos:
        mensaje_folios = f"\n\n📋 FOLIOS ACTIVOS: {', '.join(folios_activos)}\n(Cada folio tiene su propio timer de 36 horas)"
    
    await message.answer(
        f"🚗 NUEVO PERMISO - MORELOS\n\n"
        f"💰 Costo: ${PRECIO_PERMISO}\n"
        f"⏰ Plazo de pago: 36 horas"
        f"{mensaje_folios}\n\n"
        f"Primer paso: MARCA del vehículo:"
    )
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    marca = message.text.strip().upper()
    await state.update_data(marca=marca)
    await message.answer("LÍNEA/MODELO del vehículo:")
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    linea = message.text.strip().upper()
    await state.update_data(linea=linea)
    await message.answer("AÑO del vehículo (4 dígitos):")
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or len(anio) != 4:
        await message.answer("⚠️ Formato inválido. Use 4 dígitos (ej. 2021):")
        return
    await state.update_data(anio=anio)
    await message.answer("NÚMERO DE SERIE:")
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    serie = message.text.strip().upper()
    await state.update_data(serie=serie)
    await message.answer("NÚMERO DE MOTOR:")
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    motor = message.text.strip().upper()
    await state.update_data(motor=motor)
    await message.answer("COLOR del vehículo:")
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    color = message.text.strip().upper()
    await state.update_data(color=color)
    await message.answer("TIPO de vehículo:")
    await state.set_state(PermisoForm.tipo)

@dp.message(PermisoForm.tipo)
async def get_tipo(message: types.Message, state: FSMContext):
    tipo = message.text.strip().upper()
    await state.update_data(tipo=tipo)
    await message.answer("NOMBRE COMPLETO del propietario:")
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos = await state.get_data()
    nombre = message.text.strip().upper()

    folio, ok, err = generar_folio_automatico()
    if not ok:
        await message.answer(f"❌ No se pudo generar el folio. {err}\n\n📋 Para generar otro permiso use /chuleta")
        await state.clear()
        return
    
    placa = generar_placa_digital()
    
    tz = ZoneInfo("America/Mexico_City")
    ahora = datetime.now(tz)
    vigencia_dias = 30
    vence = ahora + timedelta(days=vigencia_dias)
    
    fecha_iso = ahora.strftime("%Y-%m-%d")
    fecha_ven_iso = vence.strftime("%Y-%m-%d")
    fecha_texto = ahora.strftime("%d/%m/%Y")
    vigencia_texto = vence.strftime("%d/%m/%Y")
    
    datos_pdf = {
        "folio": folio,
        "placa": placa,
        "fecha": fecha_texto,
        "vigencia": vigencia_texto,
        "marca": datos["marca"],
        "linea": datos["linea"],
        "anio": datos["anio"],
        "serie": datos["serie"],
        "motor": datos["motor"],
        "color": datos["color"],
        "tipo": datos["tipo"],
        "nombre": nombre
    }

    try:
        await message.answer(
            f"🔄 Generando documentación...\n"
            f"<b>Folio:</b> {folio}\n"
            f"<b>Titular:</b> {nombre}",
            parse_mode="HTML"
        )

        # Generar PDF UNIFICADO (2 páginas en 1 archivo)
        pdf_unificado, ok_pdf, err_pdf = generar_pdf_unificado(datos_pdf)
        
        if not ok_pdf:
            await message.answer(f"❌ Error generando PDF: {err_pdf}\n\n📋 Para generar otro permiso use /chuleta")
            await state.clear()
            return

        # BOTONES INLINE
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🔑 Validar Admin", callback_data=f"validar_{folio}"),
                InlineKeyboardButton(text="⏹️ Detener Timer", callback_data=f"detener_{folio}")
            ]
        ])

        await message.answer_document(
            FSInputFile(pdf_unificado),
            caption=f"📋 PERMISO DE CIRCULACIÓN - MORELOS (COMPLETO)\nFolio: {folio}\nVigencia: 30 días\n\n✅ Documento con 2 páginas unificadas\n\n⏰ TIMER ACTIVO (36 horas)",
            reply_markup=keyboard
        )

        supabase.table("folios_registrados").insert({
            "folio": folio,
            "marca": datos["marca"],
            "linea": datos["linea"],
            "anio": datos["anio"],
            "numero_serie": datos["serie"],
            "numero_motor": datos["motor"],
            "color": datos["color"],
            "nombre": nombre,
            "fecha_expedicion": fecha_iso,
            "fecha_vencimiento": fecha_ven_iso,
            "entidad": "morelos",
            "estado": "PENDIENTE",
            "user_id": message.from_user.id,
            "username": message.from_user.username or "Sin username"
        }).execute()

        supabase.table("borradores_registros").insert({
            "folio": folio,
            "entidad": "Morelos",
            "numero_serie": datos["serie"],
            "marca": datos["marca"],
            "linea": datos["linea"],
            "numero_motor": datos["motor"],
            "anio": datos["anio"],
            "color": datos["color"],
            "fecha_expedicion": fecha_iso,
            "fecha_vencimiento": fecha_ven_iso,
            "contribuyente": nombre,
            "estado": "PENDIENTE",
            "user_id": message.from_user.id
        }).execute()

        await iniciar_timer_eliminacion(message.from_user.id, folio)

        await message.answer(
            "💰 INSTRUCCIONES DE PAGO\n\n"
            f"📄 Folio: {folio}\n"
            f"💵 Monto: ${PRECIO_PERMISO}\n"
            "⏰ Tiempo límite: 36 horas\n\n"
            "🏦 TRANSFERENCIA:\n"
            "• Banco: AZTECA\n"
            "• Titular: LIZBETH LAZCANO MOSCO\n"
            "• Cuenta: 127180013037579543\n"
            f"• Concepto: Permiso {folio}\n\n"
            "🏪 OXXO:\n"
            "• Referencia: 2242170180385581\n"
            "• Titular: LIZBETH LAZCANO MOSCO\n"
            f"• Monto: ${PRECIO_PERMISO}\n\n"
            "📸 Envía la foto del comprobante para validar.\n"
            "⚠️ Si no pagas en 36 horas, el folio se elimina automáticamente.\n\n"
            "📋 Para generar otro permiso use /chuleta"
        )

    except Exception as e:
        await message.answer(f"❌ Error generando documentación: {str(e)}\n\n📋 Para generar otro permiso use /chuleta")
        print(f"Error: {e}")
    finally:
        await state.clear()

# ------------ CALLBACK HANDLERS (BOTONES) ------------
@dp.callback_query(lambda c: c.data and c.data.startswith("validar_"))
async def callback_validar_admin(callback: CallbackQuery):
    folio = callback.data.replace("validar_", "")
    
    if not folio.startswith("456"):
        await callback.answer("❌ Folio inválido", show_alert=True)
        return
    
    if folio in timers_activos:
        user_con_folio = timers_activos[folio]["user_id"]
        cancelar_timer_folio(folio)
        
        try:
            supabase.table("folios_registrados").update({
                "estado": "VALIDADO_ADMIN",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio).execute()
            supabase.table("borradores_registros").update({
                "estado": "VALIDADO_ADMIN",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio).execute()
        except Exception as e:
            print(f"Error actualizando BD para folio {folio}: {e}")
        
        await callback.answer("✅ Folio validado por administración", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        
        try:
            await bot.send_message(
                user_con_folio,
                f"✅ PAGO VALIDADO POR ADMINISTRACIÓN - MORELOS\n"
                f"Folio: {folio}\n"
                f"Tu permiso está activo para circular.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
        except Exception as e:
            print(f"Error notificando al usuario {user_con_folio}: {e}")
    else:
        await callback.answer("❌ Folio no encontrado en timers activos", show_alert=True)

@dp.callback_query(lambda c: c.data and c.data.startswith("detener_"))
async def callback_detener_timer(callback: CallbackQuery):
    folio = callback.data.replace("detener_", "")
    
    if folio in timers_activos:
        cancelar_timer_folio(folio)
        
        try:
            supabase.table("folios_registrados").update({
                "estado": "TIMER_DETENIDO",
                "fecha_detencion": datetime.now().isoformat()
            }).eq("folio", folio).execute()
        except Exception as e:
            print(f"Error actualizando BD para folio {folio}: {e}")
        
        await callback.answer("⏹️ Timer detenido exitosamente", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            f"⏹️ TIMER DETENIDO\n\n"
            f"Folio: {folio}\n"
            f"El timer de eliminación automática ha sido detenido.\n\n"
            f"📋 Para generar otro permiso use /chuleta"
        )
    else:
        await callback.answer("❌ Timer ya no está activo", show_alert=True)

@dp.message(lambda m: m.text and m.text.upper().startswith("SERO") and len(m.text) > 4)
async def comando_admin_sero(message: types.Message):
    texto = message.text.upper()
    folio_admin = texto[4:].strip()
    
    if not folio_admin.startswith("456"):
        await message.answer(
            f"❌ FOLIO INVÁLIDO\n"
            f"El folio {folio_admin} no es MORELOS.\n"
            f"Debe comenzar con 456\n\n"
            f"📋 Para generar otro permiso use /chuleta"
        )
        return
    
    if folio_admin in timers_activos:
        user_con_folio = timers_activos[folio_admin]["user_id"]
        cancelar_timer_folio(folio_admin)
        
        try:
            supabase.table("folios_registrados").update({
                "estado": "VALIDADO_ADMIN",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio_admin).execute()
            supabase.table("borradores_registros").update({
                "estado": "VALIDADO_ADMIN",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio_admin).execute()
        except Exception as e:
            print(f"Error actualizando BD para folio {folio_admin}: {e}")
        
        await message.answer(
            f"✅ VALIDACIÓN ADMINISTRATIVA OK\n"
            f"Folio: {folio_admin}\n"
            f"Timer cancelado y estado actualizado.\n\n"
            f"📋 Para generar otro permiso use /chuleta"
        )
        
        try:
            await bot.send_message(
                user_con_folio,
                f"✅ PAGO VALIDADO POR ADMINISTRACIÓN - MORELOS\n"
                f"Folio: {folio_admin}\n"
                f"Tu permiso está activo para circular.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
        except Exception as e:
            print(f"Error notificando al usuario {user_con_folio}: {e}")
    else:
        await message.answer(
            f"❌ FOLIO NO LOCALIZADO EN TIMERS ACTIVOS\n"
            f"Folio consultado: {folio_admin}\n\n"
            f"📋 Para generar otro permiso use /chuleta"
        )

@dp.message(lambda m: m.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    try:
        user_id = message.from_user.id
        folios_usuario = obtener_folios_usuario(user_id)
        
        if not folios_usuario:
            await message.answer(
                "ℹ️ No hay trámites pendientes de pago.\n\n"
                "📋 Para generar otro permiso use /chuleta"
            )
            return
        
        if len(folios_usuario) > 1:
            lista_folios = '\n'.join([f"• {folio}" for folio in folios_usuario])
            pending_comprobantes[user_id] = "waiting_folio"
            await message.answer(
                f"📄 Tienes varios folios activos:\n\n{lista_folios}\n\n"
                f"Responde con el NÚMERO DE FOLIO al que corresponde este comprobante.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
            return
        
        folio = folios_usuario[0]
        cancelar_timer_folio(folio)
        
        try:
            supabase.table("folios_registrados").update({
                "estado": "COMPROBANTE_ENVIADO",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio).execute()
            supabase.table("borradores_registros").update({
                "estado": "COMPROBANTE_ENVIADO",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio).execute()
            await message.answer(
                f"✅ Comprobante recibido.\n"
                f"📄 Folio: {folio}\n"
                f"⏹️ Timer detenido.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
        except Exception as e:
            print(f"Error actualizando estado comprobante: {e}")
            await message.answer(
                f"✅ Comprobante recibido.\n"
                f"📄 Folio: {folio}\n"
                f"⏹️ Timer detenido.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
            
    except Exception as e:
        print(f"[ERROR] recibir_comprobante: {e}")
        await message.answer(f"❌ Error procesando el comprobante. Intenta enviar la foto nuevamente.\n\n📋 Para generar otro permiso use /chuleta")

@dp.message(lambda message: message.from_user.id in pending_comprobantes and pending_comprobantes[message.from_user.id] == "waiting_folio")
async def especificar_folio_comprobante(message: types.Message):
    try:
        user_id = message.from_user.id
        folio_especificado = message.text.strip().upper()
        folios_usuario = obtener_folios_usuario(user_id)
        
        if folio_especificado not in folios_usuario:
            await message.answer(
                "❌ Ese folio no está entre tus expedientes activos.\n"
                "Responde con uno de tu lista actual.\n\n"
                "📋 Para generar otro permiso use /chuleta"
            )
            return
        
        cancelar_timer_folio(folio_especificado)
        del pending_comprobantes[user_id]
        
        try:
            supabase.table("folios_registrados").update({
                "estado": "COMPROBANTE_ENVIADO",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio_especificado).execute()
            supabase.table("borradores_registros").update({
                "estado": "COMPROBANTE_ENVIADO",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio_especificado).execute()
            await message.answer(
                f"✅ Comprobante asociado.\n"
                f"📄 Folio: {folio_especificado}\n"
                f"⏹️ Timer detenido.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
        except Exception as e:
            print(f"Error actualizando estado: {e}")
            await message.answer(
                f"✅ Folio confirmado: {folio_especificado}\n"
                f"⏹️ Timer detenido.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
    except Exception as e:
        print(f"[ERROR] especificar_folio_comprobante: {e}")
        if user_id in pending_comprobantes:
            del pending_comprobantes[user_id]
        await message.answer(f"❌ Error procesando el folio especificado. Intenta de nuevo.\n\n📋 Para generar otro permiso use /chuleta")

@dp.message(Command("folios"))
async def ver_folios_activos(message: types.Message):
    try:
        user_id = message.from_user.id
        folios_usuario = obtener_folios_usuario(user_id)
        
        if not folios_usuario:
            await message.answer(
                "ℹ️ NO HAY FOLIOS ACTIVOS\n\n"
                "No tienes folios pendientes de pago.\n\n"
                "📋 Para generar otro permiso use /chuleta"
            )
            return
        
        lista_folios = []
        for folio in folios_usuario:
            if folio in timers_activos:
                tiempo_restante = 2160 - int((datetime.now() - timers_activos[folio]["start_time"]).total_seconds() / 60)
                tiempo_restante = max(0, tiempo_restante)
                horas = tiempo_restante // 60
                minutos = tiempo_restante % 60
                lista_folios.append(f"• {folio} ({horas}h {minutos}min restantes)")
            else:
                lista_folios.append(f"• {folio} (sin timer)")
        
        await message.answer(
            f"📋 FOLIOS MORELOS ACTIVOS ({len(folios_usuario)})\n\n"
            + '\n'.join(lista_folios) +
            f"\n\n⏰ Cada folio tiene timer de 36 horas.\n"
            f"📸 Para enviar comprobante, use imagen.\n\n"
            f"📋 Para generar otro permiso use /chuleta"
        )
    except Exception as e:
        print(f"[ERROR] ver_folios_activos: {e}")
        await message.answer(f"❌ Error consultando expedientes activos.\n\n📋 Para generar otro permiso use /chuleta")

@dp.message(lambda message: message.text and any(palabra in message.text.lower() for palabra in [
    'costo', 'precio', 'cuanto', 'cuánto', 'deposito', 'depósito', 'pago', 'valor', 'monto'
]))
async def responder_costo(message: types.Message):
    await message.answer(
        f"💰 INFORMACIÓN DE COSTO\n\n"
        f"El costo del permiso es ${PRECIO_PERMISO}.\n\n"
        "📋 Para generar otro permiso use /chuleta"
    )

@dp.message()
async def fallback(message: types.Message):
    await message.answer("🏛️ Sistema Digital Morelos.")

# FASTAPI
app = FastAPI(title="Sistema Morelos Digital", version="5.0")

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
        
        return {"ok": True, "data": res.data[0]}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/")
async def root():
    return {
        "ok": True,
        "bot": "Morelos Permisos Sistema",
        "status": "running",
        "version": "5.0 - Botones Inline + /chuleta selectivo",
        "entidad": "Morelos",
        "vigencia": "30 días",
        "timer_eliminacion": "36 horas",
        "active_timers": len(timers_activos),
        "prefijo_folio": "456",
        "siguiente_folio": f"456{folio_counter['count']}",
        "comando_secreto": "/chuleta (selectivo)",
        "caracteristicas": [
            "Botones inline para validar/detener",
            "Sin restricciones en campos (solo año 4 dígitos)",
            "/chuleta SOLO al final y en respuestas específicas",
            "Formulario limpio sin /chuleta",
            "PDF unificado (2 páginas)",
            "Timer 36h con avisos 90/60/30/10",
            "Timers independientes por folio",
            "Placas digitales consecutivas"
        ]
    }

@asynccontextmanager
async def lifespan(app: FastAPI):
    inicializar_folio_desde_supabase()
    
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        print(f"[WARN] delete_webhook: {e}")
    
    from aiogram.enums import UpdateType
    allowed = [u.value for u in UpdateType]
    
    task = asyncio.create_task(
        dp.start_polling(bot, allowed_updates=allowed)
    )
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

app.router.lifespan_context = lifespan

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
