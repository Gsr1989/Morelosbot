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

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
URL_CONSULTA_BASE_MORELOS = "https://tlapadecomonfortexpediciondepermisosgob2.onrender.com"
OUTPUT_DIR = "documentos"
PLANTILLA_PDF = "morelos_hoja1_imagen.pdf"
PLANTILLA_BUENO = "morelosvergas1.pdf"
PRECIO_PERMISO = 200

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
    "fecha_hoja2": (126,310,15,(0,0,0))
}

Meses en español

meses_es = {
"January": "ENERO", "February": "FEBRERO", "March": "MARZO",
"April": "ABRIL", "May": "MAYO", "June": "JUNIO",
"July": "JULIO", "August": "AGOSTO", "September": "SEPTIEMBRE",
"October": "OCTUBRE", "November": "NOVIEMBRE", "December": "DICIEMBRE"
}

os.makedirs(OUTPUT_DIR, exist_ok=True)

------------ SUPABASE ------------

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

------------ BOT ------------

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

------------ TIMER MANAGEMENT - 12 HORAS CON TIMERS INDEPENDIENTES ------------

timers_activos = {}  # {folio: {"task": task, "user_id": user_id, "start_time": datetime}}
user_folios = {}     # {user_id: [lista_de_folios_activos]}
pending_comprobantes = {}  # Para manejar múltiples folios

------------ QR DINÁMICO PARA MORELOS ------------

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
# Obtener user_id del folio
user_id = None
if folio in timers_activos:
user_id = timers_activos[folio]["user_id"]

# Eliminar de base de datos  
    supabase.table("folios_registrados").delete().eq("folio", folio).execute()  
    supabase.table("borradores_registros").delete().eq("folio", folio).execute()  
      
    # Notificar al usuario si está disponible  
    if user_id:  
        await bot.send_message(  
            user_id,  
            f"**⏰ TIEMPO AGOTADO**\n\n"  
            f"**El folio {folio} ha sido eliminado del sistema por falta de pago.**\n\n"  
            f"Para tramitar un nuevo permiso utilize **/permiso**",  
            parse_mode="Markdown"  
        )  
      
    # Limpiar timers  
    limpiar_timer_folio(folio)  
          
except Exception as e:  
    print(f"Error eliminando folio {folio}: {e}")

async def enviar_recordatorio(folio: str, minutos_restantes: int):
"""Envía recordatorios de pago con formato de negritas"""
try:
if folio not in timers_activos:
return  # Timer ya fue cancelado

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

# Recordatorios cada 2 horas durante las primeras 10 horas  
    for horas in [2, 4, 6, 8, 10]:  
        await asyncio.sleep(2 * 60 * 60)  # 2 horas  
          
        # Verificar si el timer sigue activo  
        if folio not in timers_activos:  
            print(f"[TIMER] Cancelado para folio {folio}")  
            return  # Timer cancelado (usuario pagó)  
              
        horas_restantes = 12 - horas  
        await enviar_recordatorio(folio, horas_restantes * 60)  # Convertir a minutos  
      
    # Recordatorio final a las 11.5 horas (faltan 30 minutos)  
    await asyncio.sleep(1.5 * 60 * 60)  # 1.5 horas más  
    if folio in timers_activos:  
        await enviar_recordatorio(folio, 30)  # 30 minutos restantes  
      
    # Esperar 30 minutos finales  
    await asyncio.sleep(30 * 60)  
      
    # Si llegamos aquí, se acabó el tiempo (12 horas completas)  
    if folio in timers_activos:  
        print(f"[TIMER] Expirado para folio {folio} después de 12 HORAS")  
        await eliminar_folio_automatico(folio)  
  
# Crear y guardar el task  
task = asyncio.create_task(timer_task())  
timers_activos[folio] = {  
    "task": task,  
    "user_id": user_id,  
    "start_time": datetime.now()  
}  
  
# Agregar folio a la lista del usuario  
if user_id not in user_folios:  
    user_folios[user_id] = []  
user_folios[user_id].append(folio)  
  
print(f"[SISTEMA] Timer de 12 HORAS iniciado para folio {folio}, total timers activos: {len(timers_activos)}")

def cancelar_timer_folio(folio: str):
"""Cancela el timer de un folio específico cuando el usuario paga"""
if folio in timers_activos:
timers_activos[folio]["task"].cancel()
user_id = timers_activos[folio]["user_id"]

# Remover de estructuras de datos  
    del timers_activos[folio]  
      
    if user_id in user_folios and folio in user_folios[user_id]:  
        user_folios[user_id].remove(folio)  
        if not user_folios[user_id]:  # Si no quedan folios, eliminar entrada  
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

------------ FOLIO SYSTEM CON PREFIJO 345 ------------

folio_counter = {"count": 1}

def inicializar_folio_desde_supabase():
"""Inicializa el contador de folios desde el último registro en Supabase con prefijo 345"""
try:
response = supabase.table("folios_registrados") \
.select("folio") \
.eq("entidad", "morelos") \
.order("folio", desc=True) \
.limit(1) \
.execute()

if response.data:  
        ultimo_folio = response.data[0]["folio"]  
        # Extraer número del folio (eliminar prefijo "345")  
        if ultimo_folio.startswith("345") and len(ultimo_folio) > 3:  
            try:  
                numero = int(ultimo_folio[3:])  # Quitar "345" del inicio  
                folio_counter["count"] = numero + 1  
                print(f"[INFO] Folio Morelos inicializado desde Supabase: {ultimo_folio}, siguiente: 345{folio_counter['count']}")  
            except ValueError:  
                print("[ERROR] Formato de folio inválido en BD, iniciando desde 3451")  
                folio_counter["count"] = 1  
        else:  
            print("[INFO] No hay folios con prefijo 345, iniciando desde 3451")  
            folio_counter["count"] = 1  
    else:  
        print("[INFO] No se encontraron folios de Morelos, iniciando desde 3451")  
        folio_counter["count"] = 1  
          
    print(f"[SISTEMA] Próximo folio a generar: 345{folio_counter['count']}")  
      
except Exception as e:  
    print(f"[ERROR CRÍTICO] Al inicializar folio Morelos: {e}")  
    folio_counter["count"] = 1  
    print("[FALLBACK] Iniciando contador desde 3451")

def generar_folio_automatico() -> tuple:
"""
Genera folio automático con prefijo 345
Returns: (folio_generado: str, success: bool, error_msg: str)
"""
max_intentos = 5

for intento in range(max_intentos):  
    folio = f"345{folio_counter['count']}"  
      
    try:  
        # Verificar si el folio ya existe en la BD  
        response = supabase.table("folios_registrados") \  
            .select("folio") \  
            .eq("folio", folio) \  
            .execute()  
          
        if response.data:  
            # Folio duplicado, incrementar contador y reintentar  
            print(f"[WARNING] Folio {folio} duplicado, incrementando contador...")  
            folio_counter["count"] += 1  
            continue  
          
        # Folio disponible  
        folio_counter["count"] += 1  
        print(f"[SUCCESS] Folio generado: {folio}")  
        return folio, True, ""  
          
    except Exception as e:  
        print(f"[ERROR] Al verificar folio {folio}: {e}")  
        folio_counter["count"] += 1  
        continue  
  
# Si llegamos aquí, fallaron todos los intentos  
error_msg = f"Sistema sobrecargado, no se pudo generar folio único después de {max_intentos} intentos"  
print(f"[ERROR CRÍTICO] {error_msg}")  
return "", False, error_msg

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
    # Fallback: generar placa aleatoria  
    letras = ''.join(random.choices(abc, k=3))  
    numeros = ''.join(random.choices('0123456789', k=4))  
    return f"{letras}{numeros}"

------------ FSM STATES ------------

class PermisoForm(StatesGroup):
marca = State()
linea = State()
anio = State()
serie = State()
motor = State()
color = State()
tipo = State()
nombre = State()

------------ PDF FUNCTIONS CON QR DINÁMICO ------------

def generar_pdf_principal(datos: dict) -> tuple:
"""
Genera PDF principal con QR dinámico
Returns: (filename: str, success: bool, error_msg: str)
"""
try:
doc = fitz.open(PLANTILLA_PDF)
pg = doc[0]

# Usar coordenadas de Morelos  
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

    # Segunda página: QR DINÁMICO (MODIFICACIÓN PRINCIPAL)  
    if len(doc) > 1:  
        pg2 = doc[1]  

        # Insertar vigencia en hoja 2  
        pg2.insert_text(  
            coords_morelos["fecha_hoja2"][:2],  
            datos["vigencia"],  
            fontsize=coords_morelos["fecha_hoja2"][2],  
            color=coords_morelos["fecha_hoja2"][3]  
        )  

        # GENERAR QR DINÁMICO (CAMBIO PRINCIPAL)  
        img_qr, url_qr = generar_qr_dinamico_morelos(datos["folio"])  
          
        if img_qr:  
            # Convertir imagen PIL a bytes para PyMuPDF  
            buf = BytesIO()  
            img_qr.save(buf, format="PNG")  
            buf.seek(0)  
            qr_pix = fitz.Pixmap(buf.read())  

            # Insertar QR dinámico en las coordenadas existentes  
            rect_qr = fitz.Rect(665, 282, 665 + 70.87, 282 + 70.87)  # 2.5 cm x 2.5 cm  
            pg2.insert_image(  
                rect_qr,  
                pixmap=qr_pix,  
                overlay=True  
            )  
            print(f"[QR MORELOS] QR dinámico insertado en PDF: {url_qr}")  
        else:  
            # Fallback: usar texto estático si falla el QR dinámico  
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

            qr = qrcode.QRCode(  
                version=1,  
                error_correction=qrcode.constants.ERROR_CORRECT_L,  
                box_size=10,  
                border=2,  
            )  
            qr.add_data(texto_qr_fallback)  
            qr.make(fit=True)  

            qr_img = qr.make_image(fill_color="black", back_color="white")  
            buffer = BytesIO()  
            qr_img.save(buffer, format="PNG")  
            buffer.seek(0)  

            rect_qr = fitz.Rect(665, 282, 665 + 70.87, 282 + 70.87)  
            pg2.insert_image(rect_qr, stream=buffer.read())  
            print(f"[QR MORELOS] QR fallback (texto) insertado")  

    filename = f"{OUTPUT_DIR}/{datos['folio']}_morelos.pdf"  
    doc.save(filename)  
    doc.close()  
    return filename, True, ""  
      
except Exception as e:  
    error_msg = f"Error generando PDF principal: {str(e)}"  
    print(f"[ERROR PDF] {error_msg}")  
    return "", False, error_msg

def generar_pdf_bueno(folio: str, numero_serie: str, nombre: str) -> tuple:
"""
Genera PDF de comprobante
Returns: (filename: str, success: bool, error_msg: str)
"""
try:
doc = fitz.open(PLANTILLA_BUENO)
page = doc[0]

ahora = datetime.now()  
    page.insert_text((155, 245), nombre.upper(), fontsize=18, fontname="helv")  
    page.insert_text((1045, 205), folio, fontsize=20, fontname="helv")  
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

------------ DATABASE FUNCTIONS ------------

def guardar_en_database(datos: dict, fecha_iso: str, fecha_ven_iso: str, user_id: int, username: str) -> tuple:
"""
Guarda registro en base de datos
Returns: (success: bool, error_msg: str)
"""
try:
# Tabla principal
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

# Tabla borradores (compatibilidad)  
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

------------ HANDLERS CON NEGRITAS ------------

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
        "Perfecto. Ahora indique el **AÑO** del vehículo:",
        parse_mode="Markdown"
    )
    await state.set_state(PermisoForm.anio)

except Exception as e:
    print(f"[ERROR] get_linea: {e}")
    await message.answer(
        "**❌ ERROR PROCESANDO LÍNEA**\n\n"
        "Ocurrió un problema al registrar la línea.\n"
        "Por favor, utilice **/permiso** para reiniciar el proceso.",
        parse_mode="Markdown"
    )
    await state.clear()

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    try:
        anio_text = message.text.strip()
        
        # Validar que sea un año válido (4 dígitos entre 1900 y año actual + 1)
        try:
            anio = int(anio_text)
            year_actual = datetime.now().year
            if not (1900 <= anio <= year_actual + 1):
                raise ValueError("Año fuera de rango")
        except ValueError:
            await message.answer(
                "⚠️ **AÑO INVÁLIDO**\n\n"
                f"Por favor, ingrese un año válido entre 1900 y {datetime.now().year + 1}.\n"
                "Ejemplo: 2020, 2018, 2015\n\n"
                "Intente nuevamente:",
                parse_mode="Markdown"
            )
            return

        await state.update_data(anio=str(anio))
        await message.answer(
            f"**✅ AÑO CONFIRMADO:** {anio}\n\n"
            "Excelente. Ahora proporcione el **NÚMERO DE SERIE** (VIN) del vehículo:",
            parse_mode="Markdown"
        )
        await state.set_state(PermisoForm.serie)

    except Exception as e:
        print(f"[ERROR] get_anio: {e}")
        await message.answer(
            "**❌ ERROR PROCESANDO AÑO**\n\n"
            "Ocurrió un problema al registrar el año.\n"
            "Por favor, utilice **/permiso** para reiniciar el proceso.",
            parse_mode="Markdown"
        )
        await state.clear()

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    try:
        serie = message.text.strip().upper()
        if not serie or len(serie) < 8:
            await message.answer(
                "⚠️ **NÚMERO DE SERIE INVÁLIDO**\n\n"
                "El número de serie (VIN) debe tener al menos 8 caracteres.\n"
                "Ejemplo: 3N1AB61E18L123456\n\n"
                "Intente nuevamente:",
                parse_mode="Markdown"
            )
            return

        await state.update_data(serie=serie)
        await message.answer(
            f"**✅ SERIE REGISTRADA:** {serie}\n\n"
            "Perfecto. Ahora indique el **NÚMERO DE MOTOR** del vehículo:",
            parse_mode="Markdown"
        )
        await state.set_state(PermisoForm.motor)

    except Exception as e:
        print(f"[ERROR] get_serie: {e}")
        await message.answer(
            "**❌ ERROR PROCESANDO SERIE**\n\n"
            "Ocurrió un problema al registrar el número de serie.\n"
            "Por favor, utilice **/permiso** para reiniciar el proceso.",
            parse_mode="Markdown"
        )
        await state.clear()

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    try:
        motor = message.text.strip().upper()
        if not motor or len(motor) < 4:
            await message.answer(
                "⚠️ **NÚMERO DE MOTOR INVÁLIDO**\n\n"
                "El número de motor debe tener al menos 4 caracteres.\n"
                "Ejemplo: GA16DE, QG18DD, HR15DE\n\n"
                "Intente nuevamente:",
                parse_mode="Markdown"
            )
            return

        await state.update_data(motor=motor)
        await message.answer(
            f"**✅ MOTOR REGISTRADO:** {motor}\n\n"
            "Excelente. Ahora indique el **COLOR** del vehículo:",
            parse_mode="Markdown"
        )
        await state.set_state(PermisoForm.color)

    except Exception as e:
        print(f"[ERROR] get_motor: {e}")
        await message.answer(
            "**❌ ERROR PROCESANDO MOTOR**\n\n"
            "Ocurrió un problema al registrar el número de motor.\n"
            "Por favor, utilice **/permiso** para reiniciar el proceso.",
            parse_mode="Markdown"
        )
        await state.clear()

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    try:
        color = message.text.strip().upper()
        if not color or len(color) < 3:
            await message.answer(
                "⚠️ **COLOR INVÁLIDO**\n\n"
                "Por favor, ingrese un color válido de al menos 3 caracteres.\n"
                "Ejemplos: BLANCO, NEGRO, AZUL, ROJO, PLATA\n\n"
                "Intente nuevamente:",
                parse_mode="Markdown"
            )
            return

        await state.update_data(color=color)
        await message.answer(
            f"**✅ COLOR CONFIRMADO:** {color}\n\n"
            "Perfecto. Ahora indique el **TIPO DE VEHÍCULO**:\n\n"
            "**Opciones disponibles:**\n"
            "• AUTOMOVIL\n"
            "• CAMIONETA\n"
            "• MOTOCICLETA\n"
            "• CAMION\n"
            "• AUTOBUS\n\n"
            "Escriba el tipo correspondiente:",
            parse_mode="Markdown"
        )
        await state.set_state(PermisoForm.tipo)

    except Exception as e:
        print(f"[ERROR] get_color: {e}")
        await message.answer(
            "**❌ ERROR PROCESANDO COLOR**\n\n"
            "Ocurrió un problema al registrar el color.\n"
            "Por favor, utilice **/permiso** para reiniciar el proceso.",
            parse_mode="Markdown"
        )
        await state.clear()

@dp.message(PermisoForm.tipo)
async def get_tipo(message: types.Message, state: FSMContext):
    try:
        tipo = message.text.strip().upper()
        tipos_validos = ["AUTOMOVIL", "CAMIONETA", "MOTOCICLETA", "CAMION", "AUTOBUS"]
        
        if tipo not in tipos_validos:
            await message.answer(
                "⚠️ **TIPO DE VEHÍCULO INVÁLIDO**\n\n"
                "Por favor, seleccione uno de los tipos válidos:\n\n"
                "• AUTOMOVIL\n"
                "• CAMIONETA\n"
                "• MOTOCICLETA\n"
                "• CAMION\n"
                "• AUTOBUS\n\n"
                "Escriba exactamente como aparece en la lista:",
                parse_mode="Markdown"
            )
            return

        await state.update_data(tipo=tipo)
        await message.answer(
            f"**✅ TIPO CONFIRMADO:** {tipo}\n\n"
            "Finalmente, proporcione el **NOMBRE COMPLETO** del propietario del vehículo:",
            parse_mode="Markdown"
        )
        await state.set_state(PermisoForm.nombre)

    except Exception as e:
        print(f"[ERROR] get_tipo: {e}")
        await message.answer(
            "**❌ ERROR PROCESANDO TIPO**\n\n"
            "Ocurrió un problema al registrar el tipo de vehículo.\n"
            "Por favor, utilice **/permiso** para reiniciar el proceso.",
            parse_mode="Markdown"
        )
        await state.clear()

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    try:
        nombre = message.text.strip().upper()
        if not nombre or len(nombre) < 5:
            await message.answer(
                "⚠️ **NOMBRE INVÁLIDO**\n\n"
                "Por favor, ingrese el nombre completo (al menos 5 caracteres).\n"
                "Ejemplo: JUAN CARLOS PÉREZ GARCÍA\n\n"
                "Intente nuevamente:",
                parse_mode="Markdown"
            )
            return

        await state.update_data(nombre=nombre)
        
        # Obtener todos los datos del formulario
        data = await state.get_data()
        
        # Mostrar resumen para confirmación
        resumen = (
            "**📋 RESUMEN DE DATOS CAPTURADOS**\n\n"
            f"**👤 PROPIETARIO:** {nombre}\n"
            f"**🚗 MARCA:** {data['marca']}\n"
            f"**🔧 LÍNEA:** {data['linea']}\n"
            f"**📅 AÑO:** {data['anio']}\n"
            f"**🔢 No. SERIE:** {data['serie']}\n"
            f"**⚙️ No. MOTOR:** {data['motor']}\n"
            f"**🎨 COLOR:** {data['color']}\n"
            f"**🚙 TIPO:** {data['tipo']}\n\n"
            "**💰 INVERSIÓN DEL SERVICIO:** El costo es el mismo de siempre\n"
            "**⏰ TIEMPO LÍMITE PARA PAGO:** 12 horas\n\n"
            "**¿Los datos son correctos?**\n"
            "• Responda **SI** para continuar\n"
            "• Responda **NO** para reiniciar el proceso\n"
            "• Use **/permiso** para comenzar de nuevo"
        )
        
        await message.answer(resumen, parse_mode="Markdown")
        await state.update_data(esperando_confirmacion=True)

    except Exception as e:
        print(f"[ERROR] get_nombre: {e}")
        await message.answer(
            "**❌ ERROR PROCESANDO NOMBRE**\n\n"
            "Ocurrió un problema al registrar el nombre.\n"
            "Por favor, utilice **/permiso** para reiniciar el proceso.",
            parse_mode="Markdown"
        )
        await state.clear()

@dp.message()
async def handle_confirmacion_y_pagos(message: types.Message, state: FSMContext):
    try:
        data = await state.get_data()
        
        # Manejar confirmación del formulario
        if data.get("esperando_confirmacion"):
            respuesta = message.text.strip().upper()
            
            if respuesta == "SI":
                await procesar_solicitud_permiso(message, state, data)
            elif respuesta == "NO":
                await message.answer(
                    "**❌ PROCESO CANCELADO**\n\n"
                    "Los datos han sido descartados.\n"
                    "Use **/permiso** para iniciar un nuevo trámite.",
                    parse_mode="Markdown"
                )
                await state.clear()
            else:
                await message.answer(
                    "**⚠️ RESPUESTA INVÁLIDA**\n\n"
                    "Por favor, responda únicamente:\n"
                    "• **SI** para continuar con el trámite\n"
                    "• **NO** para cancelar el proceso",
                    parse_mode="Markdown"
                )
            return
        
        # Manejar comprobantes de pago (imágenes)
        if message.content_type == ContentType.PHOTO:
            await manejar_comprobante_pago(message, state)
            return
        
        # Respuesta por defecto para mensajes no reconocidos
        await message.answer(
            "**🤖 COMANDO NO RECONOCIDO**\n\n"
            "**Comandos disponibles:**\n"
            "• **/start** - Información del sistema\n"
            "• **/permiso** - Solicitar nuevo permiso\n"
            "• **/folios** - Ver folios activos\n\n"
            "**💳 PAGO:** Envíe una imagen de su comprobante de pago",
            parse_mode="Markdown"
        )

    except Exception as e:
        print(f"[ERROR] handle_confirmacion_y_pagos: {e}")
        await message.answer(
            "**❌ ERROR INTERNO**\n\n"
            "Ocurrió un problema procesando su solicitud.\n"
            "Por favor, intente nuevamente.",
            parse_mode="Markdown"
        )

async def procesar_solicitud_permiso(message: types.Message, state: FSMContext, data: dict):
    """Procesa la solicitud de permiso después de la confirmación"""
    try:
        # Generar folio automático
        folio, success, error = generar_folio_automatico()
        if not success:
            await message.answer(
                f"**❌ ERROR GENERANDO FOLIO**\n\n{error}\n\n"
                "Por favor, intente nuevamente en unos minutos.",
                parse_mode="Markdown"
            )
            await state.clear()
            return

        # Generar placa digital
        placa = generar_placa_digital()
        
        # Calcular fechas
        ahora = datetime.now(ZoneInfo("America/Mexico_City"))
        fecha_expedicion = ahora.strftime("%d/%m/%Y")
        fecha_vencimiento = (ahora + timedelta(days=30)).strftime("%d/%m/%Y")
        fecha_vencimiento_es = ahora + timedelta(days=30)
        mes_es = meses_es[fecha_vencimiento_es.strftime("%B")]
        vigencia_formato = f"{fecha_vencimiento_es.day} DE {mes_es} DEL {fecha_vencimiento_es.year}"

        # Preparar datos completos
        datos_completos = {
            "folio": folio,
            "placa": placa,
            "fecha": fecha_expedicion,
            "vigencia": vigencia_formato,
            "marca": data["marca"],
            "serie": data["serie"],
            "linea": data["linea"],
            "motor": data["motor"],
            "anio": data["anio"],
            "color": data["color"],
            "tipo": data["tipo"],
            "nombre": data["nombre"]
        }

        # Guardar en base de datos
        db_success, db_error = guardar_en_database(
            datos_completos,
            ahora.isoformat(),
            (ahora + timedelta(days=30)).isoformat(),
            message.from_user.id,
            message.from_user.username
        )

        if not db_success:
            await message.answer(
                f"**❌ ERROR EN BASE DE DATOS**\n\n{db_error}\n\n"
                "Por favor, intente nuevamente.",
                parse_mode="Markdown"
            )
            await state.clear()
            return

        # Generar PDFs
        pdf_principal, pdf_success, pdf_error = generar_pdf_principal(datos_completos)
        if not pdf_success:
            await message.answer(
                f"**❌ ERROR GENERANDO DOCUMENTO**\n\n{pdf_error}\n\n"
                "Contacte al soporte técnico.",
                parse_mode="Markdown"
            )
            await state.clear()
            return

        pdf_comprobante, comp_success, comp_error = generar_pdf_bueno(folio, data["serie"], data["nombre"])
        if not comp_success:
            print(f"[WARNING] Error generando comprobante: {comp_error}")

        # Iniciar timer de 12 horas
        await iniciar_timer_pago(message.from_user.id, folio)

        # Enviar documentos
        await message.answer(
            f"**✅ SOLICITUD PROCESADA EXITOSAMENTE**\n\n"
            f"**📋 FOLIO ASIGNADO:** {folio}\n"
            f"**🚗 PLACA DIGITAL:** {placa}\n"
            f"**📅 VIGENCIA:** {vigencia_formato}\n\n"
            f"**💰 INVERSIÓN:** El costo es el mismo de siempre\n"
            f"**⏰ TIEMPO LÍMITE:** 12 horas para efectuar el pago\n\n"
            f"**📄 Sus documentos se enviarán a continuación.**\n"
            f"**📸 Envíe su comprobante de pago (imagen) para activar el permiso.**",
            parse_mode="Markdown"
        )

        # Enviar PDF principal
        try:
            pdf_file = FSInputFile(pdf_principal)
            await message.answer_document(
                pdf_file,
                caption=f"**📄 PERMISO DE CIRCULACIÓN - FOLIO {folio}**\n\n"
                       f"**⚠️ DOCUMENTO PROVISIONAL**\n"
                       f"Se activará automáticamente al confirmar su pago.",
                parse_mode="Markdown"
            )
        except Exception as e:
            print(f"[ERROR] Enviando PDF principal: {e}")

        # Enviar PDF comprobante si existe
        if comp_success:
            try:
                comp_file = FSInputFile(pdf_comprobante)
                await message.answer_document(
                    comp_file,
                    caption=f"**📋 COMPROBANTE DE TRÁMITE - FOLIO {folio}**",
                    parse_mode="Markdown"
                )
            except Exception as e:
                print(f"[ERROR] Enviando comprobante: {e}")

        await state.clear()

    except Exception as e:
        print(f"[ERROR] procesar_solicitud_permiso: {e}")
        await message.answer(
            "**❌ ERROR CRÍTICO**\n\n"
            "No se pudo completar el proceso de solicitud.\n"
            "Por favor, contacte al soporte técnico.",
            parse_mode="Markdown"
        )
        await state.clear()

async def manejar_comprobante_pago(message: types.Message, state: FSMContext):
    """Maneja los comprobantes de pago enviados por los usuarios"""
    try:
        user_id = message.from_user.id
        folios_activos = obtener_folios_usuario(user_id)
        
        if not folios_activos:
            await message.answer(
                "**❌ NO TIENE FOLIOS ACTIVOS**\n\n"
                "No se encontraron folios pendientes de pago asociados a su cuenta.\n"
                "Use **/permiso** para solicitar un nuevo trámite.",
                parse_mode="Markdown"
            )
            return

        if len(folios_activos) == 1:
            # Un solo folio, procesar directamente
            folio = folios_activos[0]
            await validar_pago_folio(message, folio)
        else:
            # Múltiples folios, solicitar especificar
            lista_folios = "\n".join([f"• **{folio}**" for folio in folios_activos])
            await message.answer(
                "**💳 COMPROBANTE DE PAGO RECIBIDO**\n\n"
                "**Tiene múltiples folios activos:**\n\n"
                f"{lista_folios}\n\n"
                "**Por favor, responda con el número de folio al que corresponde este pago.**",
                parse_mode="Markdown"
            )
            
            # Guardar el comprobante temporalmente
            pending_comprobantes[user_id] = {
                "photo": message.photo[-1].file_id,
                "timestamp": datetime.now()
            }

    except Exception as e:
        print(f"[ERROR] manejar_comprobante_pago: {e}")
        await message.answer(
            "**❌ ERROR PROCESANDO COMPROBANTE**\n\n"
            "No se pudo procesar su comprobante de pago.\n"
            "Por favor, intente nuevamente.",
            parse_mode="Markdown"
        )

async def validar_pago_folio(message: types.Message, folio: str):
    """Valida el pago para un folio específico"""
    try:
        # Verificar que el folio existe y está pendiente
        response = supabase.table("folios_registrados") \
            .select("*") \
            .eq("folio", folio) \
            .eq("estado", "PENDIENTE") \
            .single() \
            .execute()

        if not response.data:
            await message.answer(
                f"**❌ FOLIO NO ENCONTRADO O YA PROCESADO**\n\n"
                f"El folio **{folio}** no se encuentra en estado pendiente.\n"
                f"Puede que ya haya sido procesado o haya expirado.",
                parse_mode="Markdown"
            )
            return

        # Cancelar timer del folio
        cancelar_timer_folio(folio)

        # Actualizar estado en base de datos
        supabase.table("folios_registrados") \
            .update({"estado": "PAGADO"}) \
            .eq("folio", folio) \
            .execute()

        supabase.table("borradores_registros") \
            .update({"estado": "PAGADO"}) \
            .eq("folio", folio) \
            .execute()

        # Notificar éxito
        datos = response.data
        await message.answer(
            f"**✅ PAGO CONFIRMADO EXITOSAMENTE**\n\n"
            f"**📋 FOLIO:** {folio}\n"
            f"**👤 PROPIETARIO:** {datos['nombre']}\n"
            f"**🚗 VEHÍCULO:** {datos['marca']} {datos['linea']} {datos['anio']}\n"
            f"**📅 VIGENCIA:** {datos['fecha_vencimiento'][:10]}\n\n"
            f"**🎉 Su permiso de circulación ha sido ACTIVADO**\n"
            f"**📱 Puede consultar su estatus en cualquier momento**\n\n"
            f"**Gracias por utilizar nuestros servicios digitales.**",
            parse_mode="Markdown"
        )

        print(f"[PAGO EXITOSO] Folio {folio} activado para usuario {message.from_user.id}")

    except Exception as e:
        print(f"[ERROR] validar_pago_folio: {e}")
        await message.answer(
            f"**❌ ERROR VALIDANDO PAGO**\n\n"
            f"Ocurrió un problema al procesar el pago del folio **{folio}**.\n"
            f"Por favor, contacte al soporte técnico.",
            parse_mode="Markdown"
        )

@dp.message(Command("folios"))
async def folios_cmd(message: types.Message):
    """Muestra los folios activos del usuario"""
    try:
        user_id = message.from_user.id
        folios_activos = obtener_folios_usuario(user_id)
        
        if not folios_activos:
            await message.answer(
                "**📋 NO TIENE FOLIOS ACTIVOS**\n\n"
                "Actualmente no tiene folios pendientes de pago.\n"
                "Use **/permiso** para solicitar un nuevo trámite.",
                parse_mode="Markdown"
            )
            return

        # Obtener información detallada de cada folio
        info_folios = []
        for folio in folios_activos:
            if folio in timers_activos:
                timer_info = timers_activos[folio]
                tiempo_transcurrido = datetime.now() - timer_info["start_time"]
                horas_restantes = 12 - (tiempo_transcurrido.total_seconds() / 3600)
                horas_restantes = max(0, horas_restantes)
                
                info_folios.append(
                    f"**📋 FOLIO:** {folio}\n"
                    f"**⏰ TIEMPO RESTANTE:** {horas_restantes:.1f} horas\n"
                    f"**💰 ESTADO:** PENDIENTE DE PAGO"
                )

        mensaje = "**📋 SUS FOLIOS ACTIVOS**\n\n" + "\n\n".join(info_folios)
        mensaje += "\n\n**💳 Envíe una imagen de su comprobante de pago para activar cualquier folio.**"
        
        await message.answer(mensaje, parse_mode="Markdown")

    except Exception as e:
        print(f"[ERROR] folios_cmd: {e}")
        await message.answer(
            "**❌ ERROR CONSULTANDO FOLIOS**\n\n"
            "No se pudieron consultar sus folios activos.\n"
            "Intente nuevamente en unos momentos.",
            parse_mode="Markdown"
        )

# ------------ FASTAPI INTEGRATION ------------

app = FastAPI()

@app.get("/")
async def root():
    return {"message": "Bot Telegram - Sistema de Permisos Morelos", "status": "running"}

@app.post("/webhook")
async def webhook(request: Request):
    try:
        update = types.Update.model_validate(await request.json())
        await dp.feed_update(bot, update)
        return {"status": "ok"}
    except Exception as e:
        print(f"[ERROR WEBHOOK] {e}")
        return {"status": "error", "message": str(e)}

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    print("[STARTUP] Inicializando sistema...")
    inicializar_folio_desde_supabase()
    print(f"[STARTUP] Bot iniciado - Sistema de Permisos Morelos")
    print(f"[STARTUP] Próximo folio: 345{folio_counter['count']}")
    yield
    # Shutdown
    print("[SHUTDOWN] Cerrando sistema...")
    # Cancelar todos los timers activos
    for folio in list(timers_activos.keys()):
        timers_activos[folio]["task"].cancel()
    timers_activos.clear()
    user_folios.clear()
    pending_comprobantes.clear()
    print("[SHUTDOWN] Sistema cerrado correctamente")

app.router.lifespan_context = lifespan

# ------------ MAIN ------------

if __name__ == "__main__":
    import uvicorn
    print("[MAIN] Iniciando en modo desarrollo...")
    inicializar_folio_desde_supabase()
    uvicorn.run(app, host="0.0.0.0", port=8000)
