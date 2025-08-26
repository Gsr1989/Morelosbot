from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from aiogram.types import FSInputFile, ContentType
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta
from supabase import create_client, Client
import asyncio
import os
import fitz  # PyMuPDF
import string
import random

# ------------ CONFIG ------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
OUTPUT_DIR = "documentos"
PLANTILLA_PDF = "morelos_hoja1_imagen.pdf"
PLANTILLA_BUENO = "morelosvergas1.pdf"

# Precio del permiso (añadido para compatibilidad con funciones de pago)
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

# ------------ FOLIO MEJORADO CON INICIALIZACIÓN DESDE SUPABASE ------------
folio_counter = {"count": 1}

def inicializar_folio_desde_supabase():
    """Inicializa el contador de folios desde el último registro en Supabase"""
    try:
        response = supabase.table("folios_registrados") \
            .select("folio") \
            .eq("entidad", "morelos") \
            .order("folio", desc=True) \
            .limit(1) \
            .execute()

        if response.data:
            ultimo_folio = response.data[0]["folio"]
            # Extraer número del folio (eliminar prefijo "02")
            if ultimo_folio.startswith("02") and len(ultimo_folio) > 2:
                try:
                    numero = int(ultimo_folio[2:])  # Quitar "02" del inicio
                    folio_counter["count"] = numero + 1
                    print(f"[INFO] Folio Morelos inicializado desde Supabase: {ultimo_folio}, siguiente: 02{folio_counter['count']}")
                except ValueError:
                    folio_counter["count"] = 1
            else:
                folio_counter["count"] = 1
        else:
            folio_counter["count"] = 1
            print("[INFO] No se encontraron folios de Morelos, iniciando desde 021")
    except Exception as e:
        print(f"[ERROR] Al inicializar folio Morelos: {e}")
        folio_counter["count"] = 1

def generar_folio_automatico(prefijo: str) -> str:
    folio = f"{prefijo}{folio_counter['count']}"
    folio_counter["count"] += 1
    return folio

def generar_placa_digital():
    archivo = "placas_digitales.txt"
    abc = string.ascii_uppercase
    if not os.path.exists(archivo):
        with open(archivo, "w") as f:
            f.write("GSR1989\n")
    ultimo = open(archivo).read().strip().split("\n")[-1]
    pref, num = ultimo[:3], int(ultimo[3:])
    if num < 9999:
        nuevo = f"{pref}{num+1:04d}"
    else:
        l1,l2,l3 = list(pref)
        i3 = abc.index(l3)
        if i3 < 25:
            l3 = abc[i3+1]
        else:
            i2 = abc.index(l2)
            if i2 < 25:
                l2 = abc[i2+1]; l3 = "A"
            else:
                l1 = abc[(abc.index(l1)+1)%26]; l2=l3="A"
        nuevo = f"{l1}{l2}{l3}0000"
    with open(archivo,"a") as f:
        f.write(nuevo+"\n")
    return nuevo

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
def generar_pdf_principal(datos: dict) -> str:
    doc = fitz.open(PLANTILLA_PDF)
    pg = doc[0]

    # Usar coordenadas de Morelos - CONVERTIR TODO A MAYÚSCULAS
    pg.insert_text(coords_morelos["folio"][:2], datos["folio"], fontsize=coords_morelos["folio"][2], color=coords_morelos["folio"][3])
    pg.insert_text(coords_morelos["placa"][:2], datos["placa"].upper(), fontsize=coords_morelos["placa"][2], color=coords_morelos["placa"][3])
    pg.insert_text(coords_morelos["fecha"][:2], datos["fecha"].upper(), fontsize=coords_morelos["fecha"][2], color=coords_morelos["fecha"][3])
    pg.insert_text(coords_morelos["vigencia"][:2], datos["vigencia"], fontsize=coords_morelos["vigencia"][2], color=coords_morelos["vigencia"][3])
    pg.insert_text(coords_morelos["marca"][:2], datos["marca"].upper(), fontsize=coords_morelos["marca"][2], color=coords_morelos["marca"][3])
    pg.insert_text(coords_morelos["serie"][:2], datos["serie"].upper(), fontsize=coords_morelos["serie"][2], color=coords_morelos["serie"][3])
    pg.insert_text(coords_morelos["linea"][:2], datos["linea"].upper(), fontsize=coords_morelos["linea"][2], color=coords_morelos["linea"][3])
    pg.insert_text(coords_morelos["motor"][:2], datos["motor"].upper(), fontsize=coords_morelos["motor"][2], color=coords_morelos["motor"][3])
    pg.insert_text(coords_morelos["anio"][:2], datos["anio"], fontsize=coords_morelos["anio"][2], color=coords_morelos["anio"][3])
    pg.insert_text(coords_morelos["color"][:2], datos["color"].upper(), fontsize=coords_morelos["color"][2], color=coords_morelos["color"][3])
    pg.insert_text(coords_morelos["tipo"][:2], datos["tipo"].upper(), fontsize=coords_morelos["tipo"][2], color=coords_morelos["tipo"][3])
    pg.insert_text(coords_morelos["nombre"][:2], datos["nombre"].upper(), fontsize=coords_morelos["nombre"][2], color=coords_morelos["nombre"][3])

    # Segunda página si existe
    if len(doc) > 1:
        pg2 = doc[1]
        pg2.insert_text(coords_morelos["fecha_hoja2"][:2], datos["vigencia"], fontsize=coords_morelos["fecha_hoja2"][2], color=coords_morelos["fecha_hoja2"][3])

    filename = f"{OUTPUT_DIR}/{datos['folio']}_morelos.pdf"
    doc.save(filename)
    doc.close()
    return filename

def generar_pdf_bueno(folio: str, numero_serie: str, nombre: str) -> str:
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
    return filename

# ------------ HANDLERS CON DIÁLOGOS ELEGANTES ------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
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

@dp.message(Command("permiso"))
async def permiso_cmd(message: types.Message, state: FSMContext):
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

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    marca = message.text.strip().upper()
    await state.update_data(marca=marca)
    await message.answer(
        f"✅ MARCA REGISTRADA: {marca}\n\n"
        "Excelente. Ahora proporcione la LÍNEA o MODELO del vehículo:"
    )
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    linea = message.text.strip().upper()
    await state.update_data(linea=linea)
    await message.answer(
        f"✅ LÍNEA CONFIRMADA: {linea}\n\n"
        "Perfecto. Indique el AÑO de fabricación del vehículo (formato de 4 dígitos):"
    )
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or len(anio) != 4:
        await message.answer(
            "⚠️ Por favor, ingrese un año válido de 4 dígitos.\n"
            "Ejemplo correcto: 2020, 2015, 2023\n\n"
            "Favor de intentarlo nuevamente:"
        )
        return
    
    await state.update_data(anio=anio)
    await message.answer(
        f"✅ AÑO VERIFICADO: {anio}\n\n"
        "Muy bien. Proporcione el NÚMERO DE SERIE del vehículo:"
    )
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    serie = message.text.strip().upper()
    if len(serie) < 5:
        await message.answer(
            "⚠️ El número de serie parece estar incompleto.\n"
            "Por favor, verifique que haya ingresado la información completa.\n\n"
            "Intente nuevamente:"
        )
        return
        
    await state.update_data(serie=serie)
    await message.answer(
        f"✅ SERIE CAPTURADA: {serie}\n\n"
        "Correcto. Ahora indique el NÚMERO DE MOTOR:"
    )
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    motor = message.text.strip().upper()
    await state.update_data(motor=motor)
    await message.answer(
        f"✅ MOTOR REGISTRADO: {motor}\n\n"
        "Excelente. Especifique el COLOR del vehículo:"
    )
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    color = message.text.strip().upper()
    await state.update_data(color=color)
    await message.answer(
        f"✅ COLOR DOCUMENTADO: {color}\n\n"
        "Perfecto. Indique el TIPO de vehículo (automóvil, camioneta, motocicleta, etc.):"
    )
    await state.set_state(PermisoForm.tipo)

@dp.message(PermisoForm.tipo)
async def get_tipo(message: types.Message, state: FSMContext):
    tipo = message.text.strip().upper()
    await state.update_data(tipo=tipo)
    await message.answer(
        f"✅ TIPO CLASIFICADO: {tipo}\n\n"
        "Para finalizar, proporcione el NOMBRE COMPLETO del titular del vehículo:"
    )
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos = await state.get_data()
    nombre = message.text.strip().upper()
    datos["nombre"] = nombre
    datos["folio"] = generar_folio_automatico("02")
    datos["placa"] = generar_placa_digital()

    # -------- FECHAS FORMATOS --------
    fecha_exp = datetime.now()
    fecha_ven = fecha_exp + timedelta(days=30)

    datos["fecha"] = fecha_exp.strftime(f"%d DE {meses_es[fecha_exp.strftime('%B')]} DEL %Y").upper()
    datos["vigencia"] = fecha_ven.strftime("%d/%m/%Y")
    fecha_iso = fecha_exp.isoformat()
    fecha_ven_iso = fecha_ven.isoformat()
    # ---------------------------------

    await message.answer(
        f"🔄 PROCESANDO DOCUMENTACIÓN OFICIAL...\n\n"
        f"📄 Folio asignado: {datos['folio']}\n"
        f"🚗 Placa digital: {datos['placa']}\n"
        f"👤 Titular: {nombre}\n\n"
        "El sistema está generando su documentación. Por favor espere..."
    )

    try:
        p1 = generar_pdf_principal(datos)
        p2 = generar_pdf_bueno(datos["folio"], datos["serie"], datos["nombre"])

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

        # Guardar en base de datos con estado PENDIENTE
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
            "user_id": message.from_user.id,
            "username": message.from_user.username or "Sin username"
        }).execute()

        # También en la tabla borradores (compatibilidad)
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
            "user_id": message.from_user.id
        }).execute()

        # INICIAR TIMER DE PAGO
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
        await message.answer(
            f"❌ ERROR EN EL SISTEMA\n\n"
            f"Se ha presentado un inconveniente técnico: {str(e)}\n\n"
            "Por favor, intente nuevamente utilizando /permiso\n"
            "Si el inconveniente persiste, contacte al área de soporte técnico."
        )
    finally:
        await state.clear()

# ------------ CÓDIGO SECRETO ADMIN ------------
@dp.message(lambda message: message.text and message.text.strip().upper().startswith("SERO"))
async def codigo_admin(message: types.Message):
    texto = message.text.strip().upper()
    
    if len(texto) > 4:
        folio_admin = texto[4:]  # Quitar "SERO" del inicio
        
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
        else:
            await message.answer(
                f"❌ ERROR: EL TIMER SIGUE CORRIENDO\n\n"
                f"📄 Folio: {folio_admin}\n"
                f"⚠️ No se encontró ningún timer activo para este folio.\n\n"
                f"Posibles causas:\n"
                f"• El timer ya expiró automáticamente\n"
                f"• El usuario ya envió comprobante\n"
                f"• El folio no existe o es incorrecto\n"
                f"• El folio ya fue validado anteriormente"
            )
    else:
        await message.answer(
            "⚠️ FORMATO INCORRECTO\n\n"
            "Utilice el formato: SERO[número de folio]\n"
            "Ejemplo: SERO021"
        )

# Handler para recibir comprobantes de pago
@dp.message(lambda message: message.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    user_id = message.from_user.id
    
    if user_id not in timers_activos:
        await message.answer(
            "ℹ️ No se encontró ningún permiso pendiente de pago.\n\n"
            "Si desea tramitar un nuevo permiso, utilice /permiso"
        )
        return
    
    folio = timers_activos[user_id]["folio"]
    
    # Cancelar timer
    cancelar_timer(user_id)
    
    # Actualizar estado en base de datos
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
        f"⏰ Timer de pago detenido\n\n"
        f"🔍 Su comprobante está siendo verificado por nuestro equipo especializado.\n"
        f"Una vez validado el pago, su permiso quedará completamente activo.\n\n"
        f"Agradecemos su confianza en el Sistema Digital del Estado de Morelos."
        )
    # Handler para preguntas sobre costo
@dp.message(lambda message: message.text and any(palabra in message.text.lower() for palabra in [
    'costo', 'precio', 'cuanto', 'cuánto', 'deposito', 'depósito', 'pago', 'valor', 'monto'
]))
async def responder_costo(message: types.Message):
    await message.answer(
        "💰 INFORMACIÓN SOBRE LA INVERSIÓN\n\n"
        "El costo es el mismo de siempre.\n\n"
        "Para iniciar su trámite utilice /permiso"
    )

@dp.message()
async def fallback(message: types.Message):
    respuestas_elegantes = [
        "🏛️ Sistema Digital del Estado de Morelos. Para tramitar su permiso utilice /permiso",
        "📋 Plataforma automatizada de servicios. Comando disponible: /permiso",
        "⚡ Sistema en línea activo. Use /permiso para generar su documento oficial",
        "🚗 Servicio de permisos de Morelos. Inicie su proceso con /permiso"
    ]
    await message.answer(random.choice(respuestas_elegantes))

# ------------ FASTAPI + LIFESPAN ------------
_keep_task = None

async def keep_alive():
    while True:
        await asyncio.sleep(600)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keep_task
    # Inicializar contador de folios desde Supabase
    inicializar_folio_desde_supabase()
    
    await bot.delete_webhook(drop_pending_updates=True)
    if BASE_URL:
        await bot.set_webhook(f"{BASE_URL}/webhook", allowed_updates=["message"])
        _keep_task = asyncio.create_task(keep_alive())
    yield
    if _keep_task:
        _keep_task.cancel()
        with suppress(asyncio.CancelledError):
            await _keep_task
    await bot.session.close()

app = FastAPI(lifespan=lifespan)

@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = types.Update(**data)
    await dp.feed_webhook_update(bot, update)
    return {"ok": True}

@app.get("/")
async def health():
    return {"ok": True, "bot": "Morelos Permisos", "status": "running"}

if __name__ == '__main__':
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
