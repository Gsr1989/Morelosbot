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

# Precio del permiso
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
    "qr_hoja1": (400,500,70,70)  # NUEVA coordenada para QR en hoja 1
}

# Meses en español
meses_es = {
    "January": "ENERO", "February": "FEBRERO", "March": "MARZO",
    "April": "ABRIL", "May": "MAYO", "June": "JUNIO",
    "July": "JULIO", "August": "AGOSTO", "September": "SEPTIEMBRE",
    "October": "OCTUBRE", "November": "NOVIEMBRE", "December": "DICIEMBRE"
}

os.makedirs(OUTPUT_DIR, exist_ok=True)

# SUPABASE
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# BOT
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# TIMER MANAGEMENT - 12 HORAS CON TIMERS INDEPENDIENTES
timers_activos = {}
user_folios = {}
pending_comprobantes = {}

# QR DINÁMICO PARA MORELOS
def generar_qr_dinamico_morelos(folio):
    """Genera QR dinámico para Morelos con URL de consulta"""
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
    """Elimina folio automáticamente después del tiempo límite"""
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
    """Envía recordatorios de pago con formato de negritas"""
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
    """Inicia el timer de 12 HORAS con recordatorios para un folio específico"""
    async def timer_task():
        start_time = datetime.now()
        print(f"[TIMER] Iniciado para folio {folio}, usuario {user_id} - 12 HORAS")
        
        for horas in [2, 4, 6, 8, 10]:
            await asyncio.sleep(2 * 60 * 60)
            if folio not in timers_activos:
                print(f"[TIMER] Cancelado para folio {folio}")
                return
            horas_restantes = 12 - horas
            await enviar_recordatorio(folio, horas_restantes * 60)
        
        await asyncio.sleep(1.5 * 60 * 60)
        if folio in timers_activos:
            await enviar_recordatorio(folio, 30)
        
        await asyncio.sleep(30 * 60)
        
        if folio in timers_activos:
            print(f"[TIMER] Expirado para folio {folio} después de 12 HORAS")
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
    
    print(f"[SISTEMA] Timer de 12 HORAS iniciado para folio {folio}, total timers activos: {len(timers_activos)}")

def cancelar_timer_folio(folio: str):
    """Cancela el timer de un folio específico cuando el usuario paga"""
    if folio in timers_activos:
        timers_activos[folio]["task"].cancel()
        user_id = timers_activos[folio]["user_id"]
        del timers_activos[folio]
        
        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]:
                del user_folios[user_id]
        
        print(f"[SISTEMA] Timer cancelado para folio {folio}, timers restantes: {len(timers_activos)}")

def limpiar_timer_folio(folio: str):
    """Limpia todas las referencias de un folio tras expirar"""
    if folio in timers_activos:
        user_id = timers_activos[folio]["user_id"]
        del timers_activos[folio]
        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]:
                del user_folios[user_id]

def obtener_folios_usuario(user_id: int) -> list:
    """Obtiene todos los folios activos de un usuario"""
    return user_folios.get(user_id, [])

# FOLIO SYSTEM CON PREFIJO 456
folio_counter = {"count": 1}

def inicializar_folio_desde_supabase():
    """Inicializa el contador de folios desde el último registro en Supabase con prefijo 456"""
    try:
        response = supabase.table("folios_registrados") \
            .select("folio") \
            .eq("entidad", "morelos") \
            .order("folio", desc=True) \
            .limit(1) \
            .execute()

        if response.data:
            ultimo_folio = response.data[0]["folio"]
            if ultimo_folio.startswith("456") and len(ultimo_folio) > 3:
    numero = int(ultimo_folio[3:])  # Quitar "456" del inicio
    print(f"[INFO] Folio Morelos inicializado desde Supabase: {ultimo_folio}, siguiente: 456{folio_counter['count']}")
else:
    print("[INFO] No hay folios con prefijo 456, iniciando desde 4561")
        else:
            print("[INFO] No se encontraron folios de Morelos, iniciando desde 4561")
            folio_counter["count"] = 1
        print(f"[SISTEMA] Próximo folio a generar: 456{folio_counter['count']}")
    except Exception as e:
        print(f"[ERROR CRÍTICO] Al inicializar folio Morelos: {e}")
        folio_counter["count"] = 1
        print("[FALLBACK] Iniciando contador desde 4561")

def generar_folio_automatico() -> tuple:
    """
    Genera folio automático con prefijo 456 secuencial
    4561, 4562, 4563... al infinito
    Si hay duplicados, se brinca al siguiente
    """
    max_intentos = 50  # Permitir más intentos
    
    for intento in range(max_intentos):
        # Generar folio secuencial: 456 + contador
        folio = f"456{folio_counter['count']}"
        print(f"[DEBUG] Intento {intento+1}: Probando folio {folio}")
        
        try:
            # Verificar si existe en la base de datos
            response = supabase.table("folios_registrados") \
                .select("folio") \
                .eq("folio", folio) \
                .execute()
            
            print(f"[DEBUG] Respuesta Supabase: {response}")
            
            if response.data and len(response.data) > 0:
                # FOLIO DUPLICADO - BRINCAR AL SIGUIENTE
                print(f"[WARNING] Folio {folio} ya existe, brincando al siguiente...")
                folio_counter["count"] += 1  # Incrementar y probar el siguiente
                continue
            
            # FOLIO DISPONIBLE
            print(f"[SUCCESS] Folio disponible: {folio}")
            folio_counter["count"] += 1  # Incrementar para el próximo
            return folio, True, ""
            
        except Exception as e:
            print(f"[ERROR] Verificando folio {folio}: {e}")
            # En caso de error de conexión, generar sin verificar
            if intento >= 45:  # Últimos 5 intentos
                folio_final = f"456{folio_counter['count']}"
                folio_counter["count"] += 1
                print(f"[FALLBACK] Generando folio sin verificar: {folio_final}")
                return folio_final, True, ""
            
            # Probar siguiente número
            folio_counter["count"] += 1
            continue
    
    # Fallback final: usar timestamp
    import time
    timestamp = int(time.time()) % 1000000  # Últimos 6 dígitos
    folio_timestamp = f"456{timestamp}"
    print(f"[FALLBACK FINAL] Usando timestamp: {folio_timestamp}")
    return folio_timestamp, True, ""
    
def generar_placa_digital():
    """Genera placa digital para el vehículo"""
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

# PDF FUNCTIONS CON QR DINÁMICO EN HOJA 1
def generar_pdf_principal(datos: dict) -> tuple:
    """Genera PDF principal con QR dinámico EN HOJA 1"""
    try:
        doc = fitz.open(PLANTILLA_PDF)
        pg = doc[0]  # HOJA 1
        
        # Insertar todos los datos en hoja 1
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
        
        # QR DINÁMICO EN HOJA 1 (CAMBIO PRINCIPAL)
        img_qr, url_qr = generar_qr_dinamico_morelos(datos["folio"])
        if img_qr:
            buf = BytesIO()
            img_qr.save(buf, format="PNG")
            buf.seek(0)
            qr_pix = fitz.Pixmap(buf.read())
            
            # Insertar QR en HOJA 1 usando coordenadas específicas
            rect_qr = fitz.Rect(665, 282, 665 + 70.87, 282 + 70.87)  # Mismo lugar que antes pero en hoja 1
            pg.insert_image(rect_qr, pixmap=qr_pix, overlay=True)
            print(f"[QR MORELOS] QR dinámico insertado en HOJA 1: {url_qr}")
        else:
            # Fallback si falla el QR dinámico
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
            qr.add_data(texto_qr_fallback)
            qr.make(fit=True)
            qr_img = qr.make_image(fill_color="black", back_color="white")
            buffer = BytesIO()
            qr_img.save(buffer, format="PNG")
            buffer.seek(0)
            rect_qr = fitz.Rect(665, 282, 665 + 70.87, 282 + 70.87)
            pg.insert_image(rect_qr, stream=buffer.read())
            print(f"[QR MORELOS] QR fallback insertado en HOJA 1")
        
        # Si hay hoja 2, también insertar fecha ahí
        if len(doc) > 1:
            pg2 = doc[1]
            pg2.insert_text(
                coords_morelos["fecha_hoja2"][:2],
                datos["vigencia"],
                fontsize=coords_morelos["fecha_hoja2"][2],
                color=coords_morelos["fecha_hoja2"][3]
            )
        
        filename = f"{OUTPUT_DIR}/{datos['folio']}_morelos.pdf"
        doc.save(filename)
        doc.close()
        return filename, True, ""
    except Exception as e:
        error_msg = f"Error generando PDF principal: {str(e)}"
        print(f"[ERROR PDF] {error_msg}")
        return "", False, error_msg

def generar_pdf_bueno(folio: str, numero_serie: str, nombre: str) -> tuple:
    """Genera PDF de comprobante con fechas dd/mm/yyyy CDMX"""
    try:
        doc = fitz.open(PLANTILLA_BUENO)
        page = doc[0]
        
        # USAR HORARIO CDMX
        ahora = datetime.now(ZoneInfo("America/Mexico_City"))
        
        page.insert_text((155, 245), nombre.upper(), fontsize=18, fontname="helv")
        page.insert_text((1045, 205), folio, fontsize=20, fontname="helv")
        # FORMATO dd/mm/yyyy
        page.insert_text((1045, 275), ahora.strftime("%d/%m/%Y"), fontsize=20, fontname="helv")
        page.insert_text((1045, 348), ahora.strftime("%H:%M:%S"), fontsize=20, fontname="helv")
        
        filename = f"{OUTPUT_DIR}/{folio}.pdf"
        doc.save(filename)
        doc.close()
        return filename, True, ""
    except Exception as e:
        error_msg = f"Error generando PDF comprobante: {str(e)}"
        print(f"[ERROR PDF] {error_msg}")
        return "", False, error_msg

# DATABASE FUNCTIONS
def guardar_en_database(datos: dict, fecha_iso: str, fecha_ven_iso: str, user_id: int, username: str) -> tuple:
    """Guarda registro en base de datos"""
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

# HANDLERS CON NEGRITAS
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    try:
        await state.clear()
        await message.answer(
            "🏛️ Sistema Digital de Permisos del Estado de Morelos\n"
            "Plataforma oficial para la gestión de trámites vehiculares\n\n"
            "💰 Inversión del servicio: El costo es el mismo de siempre\n"
            "⏰ Tiempo límite para efectuar el pago: 12 horas\n"
            "💳 Opciones de pago: Transferencia bancaria y establecimientos OXXO\n\n"
            "📋 Para iniciar su trámite, utilice el comando /permiso\n"
            "⚠️ IMPORTANTE: Su folio será eliminado automáticamente del sistema si no realiza el pago dentro del tiempo establecido",
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"[ERROR] Comando start: {e}")
        await message.answer("❌ Error interno del sistema. Intente nuevamente en unos momentos.", parse_mode="Markdown")

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
        await message.answer(
            "**❌ ERROR INTERNO DEL SISTEMA**\n\n"
            "No fue posible iniciar el proceso de solicitud.\n"
            "Por favor, intente nuevamente en unos minutos.\n\n"
            "Si el problema persiste, contacte al soporte técnico.",
            parse_mode="Markdown"
        )

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    try:
        marca = message.text.strip().upper()
        if not marca or len(marca) < 2:
            await message.answer(
                "⚠️ MARCA INVÁLIDA\n\n"
                "Por favor, ingrese una marca válida de al menos 2 caracteres.\n"
                "Ejemplos: NISSAN, TOYOTA, HONDA, VOLKSWAGEN\n\n"
                "Intente nuevamente:",
                parse_mode="Markdown"
            )
            return
        
        await state.update_data(marca=marca)
        await message.answer(
            f"**✅ MARCA REGISTRADA:** {marca}\n\n"
            "Excelente. Ahora proporcione la **LÍNEA** o **MODELO** del vehículo:",
            parse_mode="Markdown"
        )
        await state.set_state(PermisoForm.linea)
    except Exception as e:
        print(f"[ERROR] get_marca: {e}")
        await message.answer(
            "**❌ ERROR PROCESANDO MARCA**\n\n"
            "Ocurrió un problema al registrar la marca.\n"
            "Por favor, utilice **/permiso** para reiniciar el proceso.",
            parse_mode="Markdown"
        )
        await state.clear()

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    try:
        linea = message.text.strip().upper()
        if not linea or len(linea) < 1:
            await message.answer(
                "⚠️ LÍNEA/MODELO INVÁLIDO\n\n"
                "Por favor, ingrese una línea o modelo válido.\n"
                "Ejemplos: SENTRA, TSURU, AVEO, JETTA\n\n"
                "Intente nuevamente:",
                parse_mode="Markdown"
            )
            return
        
        await state.update_data(linea=linea)
        await message.answer(
            f"**✅ LÍNEA CONFIRMADA:** {linea}\n\n"
            "Ahora, indique el **AÑO** (ej. 2012):",
            parse_mode="Markdown"
        )
        await state.set_state(PermisoForm.anio)
    except Exception as e:
        print(f"[ERROR] get_linea: {e}")
        await message.answer(
            "**❌ ERROR PROCESANDO LÍNEA/MODELO**\n\n"
            "Utilice **/permiso** para reiniciar el proceso.",
            parse_mode="Markdown"
        )
        await state.clear()

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    try:
        anio = message.text.strip()
        if not anio.isdigit() or not (1900 <= int(anio) <= datetime.now().year + 1):
            await message.answer(
                "⚠️ AÑO INVÁLIDO\n\n"
                "Ingresa un año numérico válido (ej. 2012). Intenta de nuevo:",
                parse_mode="Markdown"
            )
            return
        
        await state.update_data(anio=anio)
        await message.answer(
            f"**✅ AÑO REGISTRADO:** {anio}\n\n"
            "Ahora proporciona el **NÚMERO DE SERIE (VIN)**:",
            parse_mode="Markdown"
        )
        await state.set_state(PermisoForm.serie)
    except Exception as e:
        print(f"[ERROR] get_anio: {e}")
        await message.answer(
            "**❌ ERROR PROCESANDO AÑO**\n\n"
            "Usa **/permiso** para reiniciar.",
            parse_mode="Markdown"
        )
        await state.clear()

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    try:
        serie = message.text.strip().upper().replace(" ", "")
        if len(serie) < 5:
            await message.answer(
                "⚠️ SERIE INVÁLIDA\n\n"
                "Ingresa un número de serie (VIN) válido (mínimo 5 caracteres).",
                parse_mode="Markdown"
            )
            return
        
        await state.update_data(serie=serie)
        await message.answer(
            f"**✅ SERIE CARGADA:** {serie}\n\n"
            "Proporciona el **NÚMERO DE MOTOR**:",
            parse_mode="Markdown"
        )
        await state.set_state(PermisoForm.motor)
    except Exception as e:
        print(f"[ERROR] get_serie: {e}")
        await message.answer("**❌ Error con la serie.** Reinicia con **/permiso**.", parse_mode="Markdown")
        await state.clear()

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    try:
        motor = message.text.strip().upper()
        if len(motor) < 3:
            await message.answer(
                "⚠️ MOTOR INVÁLIDO\n\n"
                "Escribe un número de motor válido.",
                parse_mode="Markdown"
            )
            return
        
        await state.update_data(motor=motor)
        await message.answer(
            f"**✅ MOTOR REGISTRADO:** {motor}\n\n"
            "Ahora el **COLOR** del vehículo:",
            parse_mode="Markdown"
        )
        await state.set_state(PermisoForm.color)
    except Exception as e:
        print(f"[ERROR] get_motor: {e}")
        await message.answer("**❌ Error con el motor.** Reinicia con **/permiso**.", parse_mode="Markdown")
        await state.clear()

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    try:
        color = message.text.strip().upper()
        if len(color) < 3:
            await message.answer(
                "⚠️ COLOR INVÁLIDO\n\n"
                "Ingresa un color válido.",
                parse_mode="Markdown"
            )
            return
        
        await state.update_data(color=color)
        await message.answer(
            f"**✅ COLOR REGISTRADO:** {color}\n\n"
            "Indica el **TIPO** de vehículo (ej. PARTICULAR / CARGA / PASAJEROS):",
            parse_mode="Markdown"
        )
        await state.set_state(PermisoForm.tipo)
    except Exception as e:
        print(f"[ERROR] get_color: {e}")
        await message.answer("**❌ Error con el color.** Reinicia con **/permiso**.", parse_mode="Markdown")
        await state.clear()

@dp.message(PermisoForm.tipo)
async def get_tipo(message: types.Message, state: FSMContext):
    try:
        tipo = message.text.strip().upper()
        if len(tipo) < 3:
            await message.answer(
                "⚠️ TIPO INVÁLIDO\n\n"
                "Ejemplos: PARTICULAR, CARGA, PASAJEROS.",
                parse_mode="Markdown"
            )
            return
        
        await state.update_data(tipo=tipo)
        await message.answer(
            f"**✅ TIPO REGISTRADO:** {tipo}\n\n"
            "Por último, escribe el **NOMBRE COMPLETO DEL CONTRIBUYENTE**:",
            parse_mode="Markdown"
        )
        await state.set_state(PermisoForm.nombre)
    except Exception as e:
        print(f"[ERROR] get_tipo: {e}")
        await message.answer("**❌ Error con el tipo.** Reinicia con **/permiso**.", parse_mode="Markdown")
        await state.clear()

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    try:
        nombre = message.text.strip().upper()
        if len(nombre) < 5:
            await message.answer(
                "⚠️ NOMBRE INVÁLIDO\n\n"
                "Ingresa nombre y apellidos.",
                parse_mode="Markdown"
            )
            return
        
        await state.update_data(nombre=nombre)
        
        # Preparar datos finales
        folio, ok, err = generar_folio_automatico()
        if not ok:
            await message.answer(f"**❌ No se pudo generar el folio.** {err}", parse_mode="Markdown")
            await state.clear()
            return
        
        # Placa digital
        placa = generar_placa_digital()
        
        # Fechas CDMX con formato dd/mm/yyyy
        tz = ZoneInfo("America/Mexico_City")
        ahora = datetime.now(tz)
        vigencia_dias = 30
        vence = (ahora + timedelta(days=vigencia_dias))
        
        # FECHAS EN FORMATO dd/mm/yyyy
        fecha_iso = ahora.strftime("%Y-%m-%d")
        fecha_ven_iso = vence.strftime("%Y-%m-%d")
        fecha_texto = ahora.strftime("%d/%m/%Y")  # dd/mm/yyyy
        vigencia_texto = vence.strftime("%d/%m/%Y")  # dd/mm/yyyy
        
        # Cargar data previa del FSM
        data = await state.get_data()
        datos_pdf = {
            "folio": folio,
            "placa": placa,
            "fecha": fecha_texto,  # dd/mm/yyyy
            "vigencia": vigencia_texto,  # dd/mm/yyyy
            "marca": data["marca"],
            "linea": data["linea"],
            "anio": data["anio"],
            "serie": data["serie"],
            "motor": data["motor"],
            "color": data["color"],
            "tipo": data["tipo"],
            "nombre": nombre
        }
        
        # Guardar en BD
        ok_db, err_db = guardar_en_database(datos_pdf, fecha_iso, fecha_ven_iso, message.from_user.id, message.from_user.username or "")
        if not ok_db:
            await message.answer(f"**❌ Error guardando en base:** {err_db}", parse_mode="Markdown")
            await state.clear()
            return
        
        # Generar PDFs
        fn_permiso, ok1, e1 = generar_pdf_principal(datos_pdf)
        fn_comp, ok2, e2 = generar_pdf_bueno(folio, data["serie"], nombre)
        
        if not ok1 or not ok2:
            msg_err = f"**❌ Error generando PDFs**\n- Permiso: {e1}\n- Comprobante: {e2}"
            await message.answer(msg_err, parse_mode="Markdown")
            await state.clear()
            return
        
        # Iniciar timer independiente 12h
        await iniciar_timer_pago(message.from_user.id, folio)
        
        # Marcar comprobante pendiente para este folio
        pending_comprobantes[folio] = {
            "user_id": message.from_user.id,
            "created_at": ahora.isoformat()
        }
        
        # Enviar resumen + archivos
        await message.answer(
            "**✅ SOLICITUD REGISTRADA**\n\n"
            f"**Folio:** {folio}\n"
            f"**Placa digital:** {placa}\n"
            f"**Contribuyente:** {nombre}\n"
            f"**Expedición:** {fecha_texto}\n"
            f"**Vigencia:** {vigencia_texto}\n"
            f"**Entidad:** MORELOS\n\n"
            "**💳 PAGO:** El costo es el mismo de siempre. Tienes **12 horas**.\n"
            "Envía tu **comprobante de pago (foto)** respondiendo con el **folio en el mensaje**.\n\n"
            "A continuación te envío tu **comprobante** y el **permiso**:",
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

# RECEPCIÓN DE COMPROBANTES (FOTO)
@dp.message(lambda m: m.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    try:
        # Buscar folio en el caption o texto previo
        caption = (message.caption or "").upper()
        folio_detectado = ""
        for token in caption.replace("\n", " ").split():
            if token.isdigit() and token.startswith("456"):
                folio_detectado = token
                break
        
        if not folio_detectado:
            await message.reply(
                "**⚠️ Incluye el FOLIO en el mensaje/caption de la foto.**\n"
                "Ejemplo: `Comprobante folio 345123`",
                parse_mode="Markdown"
            )
            return
        
        # Validar que exista y esté pendiente
        resp = supabase.table("folios_registrados").select("*").eq("folio", folio_detectado).execute()
        if not resp.data:
            await message.reply("**❌ Folio no encontrado.** Verifica el número.", parse_mode="Markdown")
            return
        
        registro = resp.data[0]
        if registro.get("estado") == "PAGADO":
            await message.reply("**ℹ️ Ese folio ya está marcado como PAGADO.**", parse_mode="Markdown")
            return
        
        # Guardar evidencia mínima (solo marca estado)
        supabase.table("folios_registrados").update({"estado": "PAGADO"}).eq("folio", folio_detectado).execute()
        supabase.table("borradores_registros").update({"estado": "PAGADO"}).eq("folio", folio_detectado).execute()
        
        # Cancelar timer
        cancelar_timer_folio(folio_detectado)
        
        await message.reply(
            f"**✅ Comprobante recibido y validado.**\n**Folio {folio_detectado}** marcado como **PAGADO**.",
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"[ERROR] recibir_comprobante: {e}")
        await message.reply("**❌ Error procesando el comprobante.** Intenta de nuevo.", parse_mode="Markdown")

# RUTAS FASTAPI
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

# LIFESPAN: iniciar polling de Aiogram junto con FastAPI
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Inicializar folio desde supabase
    inicializar_folio_desde_supabase()
    
    # Asegurar que NO hay webhook activo (requisito para polling)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        print(f"[WARN] delete_webhook: {e}")
    
    # Arrancar polling del bot en background (Aiogram v3)
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

# MAIN LOCAL (opcional)
if __name__ == "__main__":
    import uvicorn
    # Ejecutar FastAPI + Aiogram (polling corre en lifespan)
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
