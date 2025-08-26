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

# ------------ CONFIG ------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
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
}

# Meses en español
meses_es = {
    "January": "ENERO", "February": "FEBRERO", "March": "MARZO",
    "April": "ABRIL", "May": "MAYO", "June": "JUNIO",
    "July": "JULIO", "August": "AGOSTO", "September": "SEPTIEMBRE",
    "October": "OCTUBRE", "November": "NOVIEMBRE", "December": "DICIEMBRE"
}

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ------------ SUPABASE ------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------ BOT ------------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ------------ TIMER MANAGEMENT ------------
timers_activos = {}  # {user_id: {"task": task, "folio": folio, "start_time": datetime}}

async def eliminar_folio_automatico(user_id: int, folio: str):
    """Elimina folio automáticamente después del tiempo límite"""
    try:
        # Eliminar de base de datos
        supabase.table("folios_registrados").delete().eq("folio", folio).execute()
        supabase.table("borradores_registros").delete().eq("folio", folio).execute()
        
        # Notificar al usuario
        await bot.send_message(
            user_id,
            f"⏰ TIEMPO AGOTADO\n\n"
            f"El folio {folio} ha sido eliminado del sistema por falta de pago.\n\n"
            f"Para tramitar un nuevo permiso utilize /permiso"
        )
        
        # Limpiar timer
        if user_id in timers_activos:
            del timers_activos[user_id]
            
    except Exception as e:
        print(f"Error eliminando folio {folio}: {e}")

async def enviar_recordatorio(user_id: int, folio: str, minutos_restantes: int):
    """Envía recordatorios de pago"""
    try:
        await bot.send_message(
            user_id,
            f"⚡ RECORDATORIO DE PAGO MORELOS\n\n"
            f"Folio: {folio}\n"
            f"Tiempo restante: {minutos_restantes} minutos\n"
            f"Monto: El costo es el mismo de siempre\n\n"
            f"📸 Envíe su comprobante de pago (imagen) para validar el trámite."
        )
    except Exception as e:
        print(f"Error enviando recordatorio a {user_id}: {e}")

async def iniciar_timer_pago(user_id: int, folio: str):
    """Inicia el timer de 2 horas con recordatorios"""
    async def timer_task():
        start_time = datetime.now()
        
        # Recordatorios cada 30 minutos
        for minutos in [30, 60, 90]:
            await asyncio.sleep(30 * 60)  # 30 minutos
            
            # Verificar si el timer sigue activo
            if user_id not in timers_activos:
                return  # Timer cancelado (usuario pagó)
                
            minutos_restantes = 120 - minutos
            await enviar_recordatorio(user_id, folio, minutos_restantes)
        
        # Último recordatorio a los 110 minutos (faltan 10)
        await asyncio.sleep(20 * 60)  # 20 minutos más
        if user_id in timers_activos:
            await enviar_recordatorio(user_id, folio, 10)
        
        # Esperar 10 minutos finales
        await asyncio.sleep(10 * 60)
        
        # Si llegamos aquí, se acabó el tiempo
        if user_id in timers_activos:
            await eliminar_folio_automatico(user_id, folio)
    
    # Crear y guardar el task
    task = asyncio.create_task(timer_task())
    timers_activos[user_id] = {
        "task": task,
        "folio": folio,
        "start_time": datetime.now()
    }

def cancelar_timer(user_id: int):
    """Cancela el timer cuando el usuario paga"""
    if user_id in timers_activos:
        timers_activos[user_id]["task"].cancel()
        del timers_activos[user_id]

# ------------ FOLIO SYSTEM CON PREFIJO 345 ------------
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

# ------------ PDF FUNCTIONS ------------
def generar_pdf_principal(datos: dict) -> tuple:
    """
    Genera PDF principal
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

        # Segunda página: texto + QR
        if len(doc) > 1:
            pg2 = doc[1]

            # Insertar vigencia en hoja 2
            pg2.insert_text(
                coords_morelos["fecha_hoja2"][:2],
                datos["vigencia"],
                fontsize=coords_morelos["fecha_hoja2"][2],
                color=coords_morelos["fecha_hoja2"][3]
            )

            # Generar QR
            texto_qr = (
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
            qr.add_data(texto_qr)
            qr.make(fit=True)

            qr_img = qr.make_image(fill_color="black", back_color="white")
            buffer = BytesIO()
            qr_img.save(buffer, format="PNG")
            buffer.seek(0)

            rect_qr = fitz.Rect(665, 282, 665 + 70.87, 282 + 70.87)  # 2.5 cm x 2.5 cm
            pg2.insert_image(rect_qr, stream=buffer.read())

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

# ------------ DATABASE FUNCTIONS ------------
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

# ------------ HANDLERS CON MANEJO DE ERRORES MEJORADO ------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    try:
        await state.clear()
        await message.answer(
            "🏛️ Sistema Digital de Permisos del Estado de Morelos\n"
            "Plataforma oficial para la gestión de trámites vehiculares\n\n"
            "💰 Inversión del servicio: El costo es el mismo de siempre\n"
            "⏰ Tiempo límite para efectuar el pago: 2 horas\n"
            "💳 Opciones de pago: Transferencia bancaria y establecimientos OXXO\n\n"
            "📋 Para iniciar su trámite, utilice el comando /permiso\n"
            "⚠️ IMPORTANTE: Su folio será eliminado automáticamente del sistema si no realiza el pago dentro del tiempo establecido"
        )
    except Exception as e:
        print(f"[ERROR] Comando start: {e}")
        await message.answer("❌ Error interno del sistema. Intente nuevamente en unos momentos.")

@dp.message(Command("permiso"))
async def permiso_cmd(message: types.Message, state: FSMContext):
    try:
        # Cancelar timer anterior si existe
        cancelar_timer(message.from_user.id)
        
        await message.answer(
            "🚗 SOLICITUD DE PERMISO DE CIRCULACIÓN - MORELOS\n\n"
            "📋 Inversión: El costo es el mismo de siempre\n"
            "⏰ Plazo para el pago: 2 horas\n"
            "💼 Concepto de pago: Número de folio asignado\n\n"
            "Al proceder, usted acepta que el folio será eliminado si no efectúa el pago en el tiempo estipulado.\n\n"
            "Para comenzar, por favor indique la MARCA de su vehículo:"
        )
        await state.set_state(PermisoForm.marca)
        
    except Exception as e:
        print(f"[ERROR] Comando permiso: {e}")
        await message.answer(
            "❌ ERROR INTERNO DEL SISTEMA\n\n"
            "No fue posible iniciar el proceso de solicitud.\n"
            "Por favor, intente nuevamente en unos minutos.\n\n"
            "Si el problema persiste, contacte al soporte técnico."
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
                "Intente nuevamente:"
            )
            return
            
        await state.update_data(marca=marca)
        await message.answer(
            f"✅ MARCA REGISTRADA: {marca}\n\n"
            "Excelente. Ahora proporcione la LÍNEA o MODELO del vehículo:"
        )
        await state.set_state(PermisoForm.linea)
        
    except Exception as e:
        print(f"[ERROR] get_marca: {e}")
        await message.answer(
            "❌ ERROR PROCESANDO MARCA\n\n"
            "Ocurrió un problema al registrar la marca.\n"
            "Por favor, utilice /permiso para reiniciar el proceso."
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
                "Intente nuevamente:"
            )
            return
            
        await state.update_data(linea=linea)
        await message.answer(
            f"✅ LÍNEA CONFIRMADA: {linea}\n\n"
            "Perfecto. Indique el AÑO de fabricación del vehículo (formato de 4 dígitos):"
        )
        await state.set_state(PermisoForm.anio)
        
    except Exception as e:
        print(f"[ERROR] get_linea: {e}")
        await message.answer(
            "❌ ERROR PROCESANDO LÍNEA\n\n"
            "Ocurrió un problema al registrar la línea del vehículo.\n"
            "Por favor, utilice /permiso para reiniciar el proceso."
        )
        await state.clear()

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    try:
        anio = message.text.strip()
        
        if not anio.isdigit() or len(anio) != 4:
            await message.answer(
                "⚠️ AÑO INVÁLIDO\n\n"
                "Por favor, ingrese un año válido de 4 dígitos.\n"
                "Ejemplo correcto: 2020, 2015, 2023\n\n"
                "Favor de intentarlo nuevamente:"
            )
            return
            
        anio_num = int(anio)
        if anio_num < 1980 or anio_num > datetime.now().year + 1:
            await message.answer(
                f"⚠️ AÑO FUERA DE RANGO\n\n"
                f"El año debe estar entre 1980 y {datetime.now().year + 1}.\n"
                f"Año ingresado: {anio}\n\n"
                "Por favor, verifique e intente nuevamente:"
            )
            return
        
        await state.update_data(anio=anio)
        await message.answer(
            f"✅ AÑO VERIFICADO: {anio}\n\n"
            "Muy bien. Proporcione el NÚMERO DE SERIE del vehículo:"
        )
        await state.set_state(PermisoForm.serie)
        
    except Exception as e:
        print(f"[ERROR] get_anio: {e}")
        await message.answer(
            "❌ ERROR PROCESANDO AÑO\n\n"
            "Ocurrió un problema al validar el año del vehículo.\n"
            "Por favor, utilice /permiso para reiniciar el proceso."
        )
        await state.clear()

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    try:
        serie = message.text.strip().upper()
        
        if len(serie) < 5:
            await message.answer(
                "⚠️ NÚMERO DE SERIE INCOMPLETO\n\n"
                "El número de serie debe tener al menos 5 caracteres.\n"
                "Por favor, verifique que haya ingresado la información completa.\n\n"
                "Intente nuevamente:"
            )
            return
            
        if len(serie) > 25:
            await message.answer(
                "⚠️ NÚMERO DE SERIE DEMASIADO LARGO\n\n"
                "El número de serie no puede exceder 25 caracteres.\n"
                "Por favor, verifique la información ingresada.\n\n"
                "Intente nuevamente:"
            )
            return
            
        await state.update_data(serie=serie)
        await message.answer(
            f"✅ SERIE CAPTURADA: {serie}\n\n"
            "Correcto. Ahora indique el NÚMERO DE MOTOR:"
        )
        await state.set_state(PermisoForm.motor)
        
    except Exception as e:
        print(f"[ERROR] get_serie: {e}")
        await message.answer(
            "❌ ERROR PROCESANDO NÚMERO DE SERIE\n\n"
            "Ocurrió un problema al registrar el número de serie.\n"
            "Por favor, utilice /permiso para reiniciar el proceso."
        )
        await state.clear()

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    try:
        motor = message.text.strip().upper()
        
        if len(motor) < 3:
            await message.answer(
                "⚠️ NÚMERO DE MOTOR INCOMPLETO\n\n"
                "El número de motor debe tener al menos 3 caracteres.\n"
                "Por favor, verifique la información.\n\n"
                "Intente nuevamente:"
            )
            return
            
        if len(motor) > 25:
            await message.answer(
                "⚠️ NÚMERO DE MOTOR DEMASIADO LARGO\n\n"
                "El número de motor no puede exceder 25 caracteres.\n"
                "Por favor, verifique la información ingresada.\n\n"
                "Intente nuevamente:"
            )
            return
            
        await state.update_data(motor=motor)
        await message.answer(
            f"✅ MOTOR REGISTRADO: {motor}\n\n"
            "Excelente. Especifique el COLOR del vehículo:"
        )
        await state.set_state(PermisoForm.color)
        
    except Exception as e:
        print(f"[ERROR] get_motor: {e}")
        await message.answer(
            "❌ ERROR PROCESANDO NÚMERO DE MOTOR\n\n"
            "Ocurrió un problema al registrar el número de motor.\n"
            "Por favor, utilice /permiso para reiniciar el proceso."
        )
        await state.clear()

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    try:
        color = message.text.strip().upper()
        
        if len(color) < 3:
            await message.answer(
                "⚠️ COLOR INVÁLIDO\n\n"
                "Por favor, ingrese un color válido.\n"
                "Ejemplos: ROJO, AZUL, BLANCO, NEGRO, GRIS\n\n"
                "Intente nuevamente:"
            )
            return
            
        if len(color) > 20:
            await message.answer(
                "⚠️ COLOR DEMASIADO LARGO\n\n"
                "El color no puede exceder 20 caracteres.\n"
                "Use nombres simples como: ROJO, AZUL, VERDE\n\n"
                "Intente nuevamente:"
            )
            return
            
        await state.update_data(color=color)
        await message.answer(
            f"✅ COLOR DOCUMENTADO: {color}\n\n"
            "Perfecto. Indique el TIPO de vehículo (automóvil, camioneta, motocicleta, etc.):"
        )
        await state.set_state(PermisoForm.tipo)
        
    except Exception as e:
        print(f"[ERROR] get_color: {e}")
        await message.answer(
            "❌ ERROR PROCESANDO COLOR\n\n"
            "Ocurrió un problema al registrar el color del vehículo.\n"
            "Por favor, utilice /permiso para reiniciar el proceso."
        )
        await state.clear()

@dp.message(PermisoForm.tipo)
async def get_tipo(message: types.Message, state: FSMContext):
    try:
        tipo = message.text.strip().upper()
        
        if len(tipo) < 3:
            await message.answer(
                "⚠️ TIPO DE VEHÍCULO INVÁLIDO\n\n"
                "Por favor, ingrese un tipo de vehículo válido.\n"
                "Ejemplos: AUTOMÓVIL, CAMIONETA, MOTOCICLETA, PICKUP\n\n"
                "Intente nuevamente:"
            )
            return
            
        if len(tipo) > 25:
            await message.answer(
                "⚠️ TIPO DE VEHÍCULO DEMASIADO LARGO\n\n"
                "El tipo de vehículo no puede exceder 25 caracteres.\n"
                "Use términos simples como: AUTOMÓVIL, CAMIONETA\n\n"
                "Intente nuevamente:"
            )
            return
            
        await state.update_data(tipo=tipo)
        await message.answer(
            f"✅ TIPO CLASIFICADO: {tipo}\n\n"
            "Para finalizar, proporcione el NOMBRE COMPLETO del titular del vehículo:"
        )
        await state.set_state(PermisoForm.nombre)
        
    except Exception as e:
        print(f"[ERROR] get_tipo: {e}")
        await message.answer(
            "❌ ERROR PROCESANDO TIPO DE VEHÍCULO\n\n"
            "Ocurrió un problema al registrar el tipo de vehículo.\n"
            "Por favor, utilice /permiso para reiniciar el proceso."
        )
        await state.clear()

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    try:
        datos = await state.get_data()
        nombre = message.text.strip().upper()
        
        # Validar nombre
        if len(nombre) < 5:
            await message.answer(
                "⚠️ NOMBRE INCOMPLETO\n\n"
                "Por favor, ingrese el nombre completo del titular.\n"
                "Debe incluir nombre(s) y apellido(s).\n\n"
                "Ejemplo: JUAN PÉREZ GARCÍA\n\n"
                "Intente nuevamente:"
            )
            return
            
        if len(nombre) > 60:
            await message.answer(
                "⚠️ NOMBRE DEMASIADO LARGO\n\n"
                "El nombre no puede exceder 60 caracteres.\n"
                "Por favor, verifique la información.\n\n"
                "Intente nuevamente:"
            )
            return
        
        # Verificar que tenga al menos dos palabras (nombre y apellido)
        palabras = nombre.split()
        if len(palabras) < 2:
            await message.answer(
                "⚠️ NOMBRE INCOMPLETO\n\n"
                "Por favor, proporcione al menos nombre y apellido.\n"
                "Ejemplo: MARÍA GONZÁLEZ\n\n"
                "Intente nuevamente:"
            )
            return
            
        datos["nombre"] = nombre
        
        # Generar folio con manejo de errores
        folio, folio_success, folio_error = generar_folio_automatico()
        if not folio_success:
            await message.answer(
                f"❌ ERROR GENERANDO FOLIO\n\n"
                f"El folio {folio if folio else 'desconocido'} ya está siendo utilizado en este momento.\n\n"
                "🔄 Por favor, utilice /permiso nuevamente para que el sistema le asigne el siguiente folio disponible.\n\n"
                "Esto puede ocurrir cuando varios usuarios tramitan permisos simultáneamente."
            )
            await state.clear()
            return
            
        datos["folio"] = folio
        datos["placa"] = generar_placa_digital()

        # -------- FECHAS FORMATOS --------
        try:
            fecha_exp = datetime.now()
            fecha_ven = fecha_exp + timedelta(days=30)

            datos["fecha"] = fecha_exp.strftime(f"%d DE {meses_es[fecha_exp.strftime('%B')]} DEL %Y").upper()
            datos["vigencia"] = fecha_ven.strftime("%d/%m/%Y")
            fecha_iso = fecha_exp.isoformat()
            fecha_ven_iso = fecha_ven.isoformat()
        except Exception as e:
            print(f"[ERROR] Generando fechas: {e}")
            await message.answer(
                "❌ ERROR EN FECHAS DEL SISTEMA\n\n"
                "Ocurrió un problema al calcular las fechas del permiso.\n"
                "Por favor, utilice /permiso para intentar nuevamente."
            )
            await state.clear()
            return
        # ---------------------------------

        await message.answer(
            f"🔄 PROCESANDO DOCUMENTACIÓN OFICIAL...\n\n"
            f"📄 Folio asignado: {datos['folio']}\n"
            f"🚗 Placa digital: {datos['placa']}\n"
            f"👤 Titular: {nombre}\n\n"
            "El sistema está generando su documentación. Por favor espere..."
        )

        try:
            # Generar PDFs con manejo de errores
            p1, pdf1_success, pdf1_error = generar_pdf_principal(datos)
            if not pdf1_success:
                await message.answer(
                    f"❌ ERROR GENERANDO DOCUMENTO PRINCIPAL\n\n"
                    f"Detalles técnicos: {pdf1_error}\n\n"
                    "No fue posible generar el permiso de circulación.\n"
                    "Por favor, utilice /permiso para intentar nuevamente."
                )
                await state.clear()
                return
                
            p2, pdf2_success, pdf2_error = generar_pdf_bueno(datos["folio"], datos["serie"], datos["nombre"])
            if not pdf2_success:
                await message.answer(
                    f"❌ ERROR GENERANDO COMPROBANTE\n\n"
                    f"Detalles técnicos: {pdf2_error}\n\n"
                    "No fue posible generar el comprobante de verificación.\n"
                    "Por favor, utilice /permiso para intentar nuevamente."
                )
                await state.clear()
                return

            # Enviar documentos
            await message.answer_document(
                FSInputFile(p1),
                caption=f"📋 PERMISO OFICIAL DE CIRCULACIÓN - MORELOS\n"
                       f"Folio: {datos['folio']}\n"
                       f"Placa: {datos['placa']}\n"
                       f"Vigencia: 30 días\n"
                       f"🏛️ Documento con validez oficial"
            )
            
            await message.answer_document(
                FSInputFile(p2),
                caption=f"📋 COMPROBANTE DE VERIFICACIÓN\n"
                       f"Serie: {datos['serie']}\n"
                       f"🔍 Documento complementario de autenticidad"
            )

            # Guardar en base de datos con manejo de errores
            db_success, db_error = guardar_en_database(
                datos, fecha_iso, fecha_ven_iso, 
                message.from_user.id, message.from_user.username
            )
            
            if not db_success:
                await message.answer(
                    f"⚠️ ADVERTENCIA - ERROR EN BASE DE DATOS\n\n"
                    f"Sus documentos se generaron correctamente, pero hubo un problema guardando el registro.\n\n"
                    f"Detalles técnicos: {db_error}\n\n"
                    f"📄 Su folio {datos['folio']} está activo, pero recomendamos contactar soporte si necesita validación adicional."
                )
            else:
                # INICIAR TIMER DE PAGO solo si se guardó correctamente
                await iniciar_timer_pago(message.from_user.id, datos['folio'])

            # Mensaje de instrucciones de pago con datos bancarios actualizados
            await message.answer(
                f"💰 INSTRUCCIONES PARA EL PAGO\n\n"
                f"📄 Folio: {datos['folio']}\n"
                f"💵 Monto: El costo es el mismo de siempre\n"
                f"⏰ Tiempo límite: 2 horas\n\n"
                
                "🏦 TRANSFERENCIA BANCARIA:\n"
                "• Banco: AZTECA\n"
                "• Titular: LIZBETH LAZCANO MOSCO\n"
                "• Cuenta: 127180013037579543\n"
                "• Concepto: Permiso " + datos['folio'] + "\n\n"
                
                "🏪 PAGO EN ESTABLECIMIENTOS OXXO:\n"
                "• Referencia: 2242170180385581\n"
                "• TARJETA SPIN\n"
                "• Titular: LIZBETH LAZCANO MOSCO\n"
                "• Cantidad exacta: El costo de siempre\n\n"
                
                f"📸 IMPORTANTE: Una vez efectuado el pago, envíe la fotografía de su comprobante para la validación correspondiente.\n\n"
                f"⚠️ ADVERTENCIA: Si no completa el pago en las próximas 2 horas, el folio {datos['folio']} será eliminado automáticamente del sistema."
            )
            
        except Exception as e:
            print(f"[ERROR CRÍTICO] Proceso completo: {e}")
            await message.answer(
                f"❌ ERROR CRÍTICO EN EL SISTEMA\n\n"
                f"Se ha presentado un inconveniente técnico grave durante el procesamiento.\n\n"
                f"Detalles: {str(e)}\n\n"
                "Por favor, intente nuevamente utilizando /permiso\n"
                "Si el inconveniente persiste, contacte al área de soporte técnico inmediatamente."
            )
        finally:
            await state.clear()
            
    except Exception as e:
        print(f"[ERROR] get_nombre: {e}")
        await message.answer(
            "❌ ERROR PROCESANDO NOMBRE\n\n"
            "Ocurrió un problema al procesar el nombre del titular.\n"
            "Por favor, utilice /permiso para reiniciar el proceso."
        )
        await state.clear()

# ------------ CÓDIGO SECRETO ADMIN ------------
@dp.message(lambda message: message.text and message.text.strip().upper().startswith("SERO"))
async def codigo_admin(message: types.Message):
    try:
        texto = message.text.strip().upper()
        
        if len(texto) <= 4:
            await message.answer(
                "⚠️ FORMATO INCORRECTO\n\n"
                "Utilice el formato: SERO[número de folio]\n"
                "Ejemplo: SERO3451234\n\n"
                "El folio debe incluir el prefijo 345."
            )
            return
            
        folio_admin = texto[4:]  # Quitar "SERO" del inicio
        
        # Validar que el folio tenga el prefijo correcto
        if not folio_admin.startswith("345"):
            await message.answer(
                f"⚠️ FOLIO INVÁLIDO\n\n"
                f"El folio {folio_admin} no tiene el prefijo correcto.\n"
                f"Los folios de Morelos deben comenzar con 345.\n\n"
                f"Ejemplo correcto: SERO3451234"
            )
            return
        
        # Buscar si hay un timer activo con ese folio
        user_con_folio = None
        for user_id, timer_info in timers_activos.items():
            if timer_info["folio"] == folio_admin:
                user_con_folio = user_id
                break
        
        if user_con_folio:
            # Cancelar timer
            cancelar_timer(user_con_folio)
            
            # Actualizar estado en base de datos
            try:
                supabase.table("folios_registrados").update({
                    "estado": "VALIDADO_ADMIN",
                    "fecha_comprobante": datetime.now().isoformat()
                }).eq("folio", folio_admin).execute()
                
                supabase.table("borradores_registros").update({
                    "estado": "VALIDADO_ADMIN",
                    "fecha_comprobante": datetime.now().isoformat()
                }).eq("folio", folio_admin).execute()
                
                await message.answer(
                    f"✅ TIMER DEL FOLIO {folio_admin} SE DETUVO CON ÉXITO\n\n"
                    f"🔐 Código administrativo ejecutado correctamente\n"
                    f"⏰ Timer cancelado exitosamente\n"
                    f"📄 Estado actualizado a VALIDADO_ADMIN\n"
                    f"👤 Usuario ID: {user_con_folio}\n\n"
                    f"El usuario ha sido notificado automáticamente."
                )
                
                # Notificar al usuario
                try:
                    await bot.send_message(
                        user_con_folio,
                        f"✅ PAGO VALIDADO POR ADMINISTRACIÓN\n\n"
                        f"📄 Folio: {folio_admin}\n"
                        f"Su permiso ha sido validado por la administración.\n"
                        f"El documento está completamente activo para su uso.\n\n"
                        f"Gracias por utilizar el Sistema Digital del Estado de Morelos."
                    )
                except Exception as e:
                    print(f"Error notificando al usuario {user_con_folio}: {e}")
                    await message.answer(
                        f"⚠️ Usuario notificado con problemas\n"
                        f"Timer detenido correctamente, pero hubo un problema enviando la notificación al usuario."
                    )
                    
            except Exception as e:
                print(f"Error actualizando BD para folio {folio_admin}: {e}")
                await message.answer(
                    f"⚠️ TIMER CANCELADO PERO ERROR EN BASE DE DATOS\n\n"
                    f"📄 Folio: {folio_admin}\n"
                    f"El timer se canceló correctamente, pero hubo un problema actualizando la base de datos.\n\n"
                    f"Detalles técnicos: {str(e)}"
                )
        else:
            await message.answer(
                f"❌ ERROR: TIMER NO ENCONTRADO\n\n"
                f"📄 Folio: {folio_admin}\n"
                f"⚠️ No se encontró ningún timer activo para este folio.\n\n"
                f"Posibles causas:\n"
                f"• El timer ya expiró automáticamente\n"
                f"• El usuario ya envió comprobante\n"
                f"• El folio no existe o es incorrecto\n"
                f"• El folio ya fue validado anteriormente\n\n"
                f"Folios activos: {len(timers_activos)}"
            )
            
    except Exception as e:
        print(f"[ERROR] codigo_admin: {e}")
        await message.answer(
            f"❌ ERROR EJECUTANDO CÓDIGO ADMINISTRATIVO\n\n"
            f"Ocurrió un problema procesando el comando.\n"
            f"Detalles técnicos: {str(e)}\n\n"
            f"Por favor, intente nuevamente o contacte soporte técnico."
        )

# Handler para recibir comprobantes de pago
@dp.message(lambda message: message.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    try:
        user_id = message.from_user.id
        
        if user_id not in timers_activos:
            await message.answer(
                "ℹ️ NO HAY PERMISOS PENDIENTES DE PAGO\n\n"
                "No se encontró ningún permiso pendiente de pago para su cuenta.\n\n"
                "Si desea tramitar un nuevo permiso, utilice /permiso"
            )
            return
        
        folio = timers_activos[user_id]["folio"]
        
        # Cancelar timer
        cancelar_timer(user_id)
        
        # Actualizar estado en base de datos
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
                f"✅ COMPROBANTE RECIBIDO CORRECTAMENTE\n\n"
                f"📄 Folio: {folio}\n"
                f"📸 Gracias por la imagen, este comprobante será revisado por un segundo filtro de verificación\n"
                f"⏰ Timer de pago detenido exitosamente\n\n"
                f"🔍 Su comprobante está siendo verificado por nuestro equipo especializado.\n"
                f"Una vez validado el pago, su permiso quedará completamente activo.\n\n"
                f"Agradecemos su confianza en el Sistema Digital del Estado de Morelos."
            )
            
        except Exception as e:
            print(f"Error actualizando estado comprobante: {e}")
            await message.answer(
                f"✅ COMPROBANTE RECIBIDO\n\n"
                f"📄 Folio: {folio}\n"
                f"📸 Su comprobante fue recibido y el timer se detuvo.\n\n"
                f"⚠️ Hubo un problema menor actualizando el estado en el sistema, pero su comprobante está guardado.\n\n"
                f"Si tiene dudas, mencione este folio: {folio}"
            )
            
    except Exception as e:
        print(f"[ERROR] recibir_comprobante: {e}")
        await message.answer(
            "❌ ERROR PROCESANDO COMPROBANTE\n\n"
            "Ocurrió un problema al procesar su imagen.\n"
            "Por favor, intente enviar nuevamente la fotografía de su comprobante.\n\n"
            "Si el problema persiste, contacte al soporte técnico."
        )

# Handler para preguntas sobre costo
@dp.message(lambda message: message.text and any(palabra in message.text.lower() for palabra in [
    'costo', 'precio', 'cuanto', 'cuánto', 'deposito', 'depósito', 'pago', 'valor', 'monto'
]))
async def responder_costo(message: types.Message):
    try:
        await message.answer(
            "💰 INFORMACIÓN SOBRE LA INVERSIÓN\n\n"
            "El costo es el mismo de siempre.\n\n"
            "Para iniciar su trámite utilice /permiso"
        )
    except Exception as e:
        print(f"[ERROR] responder_costo: {e}")
        await message.answer("💰 Para información sobre costos utilice /permiso")

@dp.message()
async def fallback(message: types.Message):
    try:
        respuestas_elegantes = [
            "🏛️ Sistema Digital del Estado de Morelos. Para tramitar su permiso utilice /permiso",
            "📋 Plataforma automatizada de servicios. Comando disponible: /permiso",
            "⚡ Sistema en línea activo. Use /permiso para generar su documento oficial",
            "🚗 Servicio de permisos de Morelos. Inicie su proceso con /permiso"
        ]
        await message.answer(random.choice(respuestas_elegantes))
    except Exception as e:
        print(f"[ERROR] fallback: {e}")
        await message.answer("🏛️ Sistema Digital de Morelos. Comando: /permiso")

# ------------ FASTAPI + LIFESPAN ------------
_keep_task = None

async def keep_alive():
    """Mantiene el sistema activo"""
    while True:
        try:
            await asyncio.sleep(600)
            print("[HEARTBEAT] Sistema activo")
        except Exception as e:
            print(f"[ERROR] keep_alive: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keep_task
    try:
        # Inicializar contador de folios desde Supabase
        print("[INICIO] Inicializando sistema...")
        inicializar_folio_desde_supabase()
        
        await bot.delete_webhook(drop_pending_updates=True)
        if BASE_URL:
            await bot.set_webhook(f"{BASE_URL}/webhook", allowed_updates=["message"])
            _keep_task = asyncio.create_task(keep_alive())
            print(f"[WEBHOOK] Configurado en {BASE_URL}/webhook")
        
        print("[SISTEMA] ¡Morelos Sistema Digital iniciado correctamente!")
        yield
        
    except Exception as e:
        print(f"[ERROR CRÍTICO] Iniciando sistema: {e}")
        yield
        
    finally:
        print("[CIERRE] Cerrando sistema...")
        if _keep_task:
            _keep_task.cancel()
            with suppress(asyncio.CancelledError):
                await _keep_task
        await bot.session.close()

app = FastAPI(lifespan=lifespan, title="Sistema Morelos Digital", version="2.0")

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = types.Update(**data)
        await dp.feed_webhook_update(bot, update)
        return {"ok": True}
    except Exception as e:
        print(f"[ERROR] webhook: {e}")
        return {"ok": False, "error": str(e)}

@app.get("/")
async def health():
    try:
        return {
            "ok": True, 
            "bot": "Morelos Permisos Sistema 345", 
            "status": "running",
            "version": "2.0",
            "next_folio": f"345{folio_counter['count']}",
            "active_timers": len(timers_activos)
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/status")
async def status_detail():
    """Endpoint de diagnóstico detallado"""
    try:
        return {
            "sistema": "Morelos Digital v2.0",
            "prefijo_folios": "345",
            "proximo_folio": f"345{folio_counter['count']}",
            "timers_activos": len(timers_activos),
            "folios_en_proceso": list(timer_info["folio"] for timer_info in timers_activos.values()),
            "timestamp": datetime.now().isoformat(),
            "status": "Operacional"
        }
    except Exception as e:
        return {"error": str(e), "status": "Error"}

if __name__ == '__main__':
    try:
        import uvicorn
        port = int(os.getenv("PORT", 8000))
        print(f"[ARRANQUE] Iniciando servidor en puerto {port}")
        uvicorn.run(app, host="0.0.0.0", port=port)
    except Exception as e:
        print(f"[ERROR FATAL] No se pudo iniciar el servidor: {e}")

