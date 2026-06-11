from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from supabase import create_client, Client
import fitz
import os
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from aiogram.types import FSInputFile, ContentType, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from contextlib import asynccontextmanager, suppress
import asyncio
import aiohttp
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
OUTPUT_DIR    = "documentos"
PLANTILLA_PDF = "morelos_hoja1_imagen.pdf"
PLANTILLA_BUENO = "morelosvergas1.pdf"

PRECIO_PERMISO = 200
TZ_MEXICO      = ZoneInfo("America/Mexico_City")

coords_morelos = {
    "folio":      (665, 282, 18, (1, 0, 0)),
    "placa":      (200, 200, 60, (0, 0, 0)),
    "fecha":      (200, 340, 14, (0, 0, 0)),
    "vigencia":   (600, 340, 14, (0, 0, 0)),
    "marca":      (110, 425, 14, (0, 0, 0)),
    "serie":      (460, 420, 14, (0, 0, 0)),
    "linea":      (110, 455, 14, (0, 0, 0)),
    "motor":      (460, 445, 14, (0, 0, 0)),
    "anio":       (110, 485, 14, (0, 0, 0)),
    "color":      (460, 395, 14, (0, 0, 0)),
    "tipo":       (510, 470, 14, (0, 0, 0)),
    "nombre":     (150, 370, 14, (0, 0, 0)),
    "fecha_hoja2":(126, 310, 15, (0, 0, 0)),
    "qr_hoja1":   (400, 500, 70, 70)
}

os.makedirs(OUTPUT_DIR, exist_ok=True)

# SUPABASE
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# BOT con timeout 300s
_bot_session = AiohttpSession(timeout=aiohttp.ClientTimeout(total=300))
bot     = Bot(token=BOT_TOKEN, session=_bot_session)
storage = MemoryStorage()
dp      = Dispatcher(storage=storage)

# TIMERS
timers_activos       = {}
user_folios          = {}
pending_comprobantes = {}
TOTAL_MINUTOS_TIMER  = 36 * 60

_folio_lock = asyncio.Lock()
_placa_lock = asyncio.Lock()
_ABC        = string.ascii_uppercase

# ── QR ────────────────────────────────────────────────────────────────────────
def generar_qr_dinamico_morelos(folio):
    try:
        url = f"{URL_CONSULTA_BASE_MORELOS}/consulta/{folio}"
        qr  = qrcode.QRCode(version=2, error_correction=qrcode.constants.ERROR_CORRECT_M,
                             box_size=4, border=1)
        qr.add_data(url); qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        print(f"[QR MORELOS] {folio} -> {url}")
        return img, url
    except Exception as e:
        print(f"[ERROR QR MORELOS] {e}")
        return None, None

# ── TIMERS ────────────────────────────────────────────────────────────────────
async def eliminar_folio_automatico(folio: str):
    try:
        user_id = timers_activos.get(folio, {}).get("user_id")
        await asyncio.to_thread(lambda: (
            supabase.table("folios_registrados").delete().eq("folio", folio).execute(),
            supabase.table("borradores_registros").delete().eq("folio", folio).execute(),
        ))
        if user_id:
            await bot.send_message(user_id,
                f"TIEMPO AGOTADO - MORELOS\n\n"
                f"El folio {folio} ha sido eliminado del sistema por no completar el pago en 36 horas.\n\n"
                f"Para generar otro permiso use /banamex")
        limpiar_timer_folio(folio)
    except Exception as e:
        print(f"Error eliminando folio {folio}: {e}")

async def enviar_recordatorio(folio: str, minutos_restantes: int):
    try:
        if folio not in timers_activos: return
        user_id = timers_activos[folio]["user_id"]
        await bot.send_message(user_id,
            f"RECORDATORIO DE PAGO - MORELOS\n\n"
            f"Folio: {folio}\n"
            f"Tiempo restante: {minutos_restantes} minutos\n"
            f"Monto: ${PRECIO_PERMISO}\n\n"
            f"Envie su comprobante de pago (imagen) para validar el tramite.\n\n"
            f"Para generar otro permiso use /banamex")
    except Exception as e:
        print(f"Error enviando recordatorio para folio {folio}: {e}")

async def iniciar_timer_eliminacion(user_id: int, folio: str, nombre: str = ""):
    async def timer_task():
        print(f"[TIMER] Iniciado folio {folio}, usuario {user_id} (36h)")
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
            print(f"[TIMER] Expirado folio {folio} - eliminando")
            await eliminar_folio_automatico(folio)

    task = asyncio.create_task(timer_task())
    timers_activos[folio] = {
        "task":       task,
        "user_id":    user_id,
        "start_time": datetime.now(),
        "nombre":     nombre,
    }
    user_folios.setdefault(user_id, []).append(folio)
    print(f"[SISTEMA] Timer 36h iniciado folio {folio} ({nombre}), total: {len(timers_activos)}")

def cancelar_timer_folio(folio: str):
    if folio in timers_activos:
        timers_activos[folio]["task"].cancel()
        user_id = timers_activos[folio]["user_id"]
        del timers_activos[folio]
        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]: del user_folios[user_id]
        print(f"[SISTEMA] Timer cancelado folio {folio}")

def limpiar_timer_folio(folio: str):
    if folio in timers_activos:
        user_id = timers_activos[folio]["user_id"]
        del timers_activos[folio]
        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]: del user_folios[user_id]

def obtener_folios_usuario(user_id: int) -> list:
    return user_folios.get(user_id, [])

# ── FOLIO SYSTEM — WATERMARK ──────────────────────────────────────────────────
FOLIO_PREFIJO_MOR  = "MOR"
FOLIO_NUM_PREFIJO  = "456"
folio_counter      = {"count": 1}
MAX_INTENTOS_FOLIO = 100_000

def _sb_leer_watermark_mor() -> int | None:
    try:
        r = supabase.table("folio_watermark") \
            .select("ultimo_asignado").eq("prefijo", FOLIO_PREFIJO_MOR).execute()
        if r.data:
            return r.data[0]["ultimo_asignado"]
        return None
    except Exception as e:
        print(f"[ERROR] leer_watermark MOR: {e}")
        return None

def _sb_guardar_watermark_mor(numero: int):
    try:
        supabase.table("folio_watermark").upsert({
            "prefijo":         FOLIO_PREFIJO_MOR,
            "ultimo_asignado": numero
        }).execute()
        print(f"[WATERMARK MOR] Guardado: {FOLIO_NUM_PREFIJO}{numero}")
    except Exception as e:
        print(f"[ERROR] guardar_watermark MOR: {e}")

def inicializar_folio_desde_supabase():
    watermark = _sb_leer_watermark_mor()
    if watermark is not None:
        folio_counter["count"] = watermark + 1
        print(f"[INFO] Folio Morelos desde watermark: {FOLIO_NUM_PREFIJO}{watermark} "
              f"-> siguiente: {folio_counter['count']}")
        return
    try:
        response = supabase.table("folios_registrados") \
            .select("folio").eq("entidad", "morelos") \
            .like("folio", f"{FOLIO_NUM_PREFIJO}%").execute()
        numeros = []
        for row in response.data or []:
            f = row.get("folio", "")
            if isinstance(f, str) and f.startswith(FOLIO_NUM_PREFIJO):
                sufijo = f[len(FOLIO_NUM_PREFIJO):]
                if sufijo.isdigit():
                    numeros.append(int(sufijo))
        if numeros:
            maximo = max(numeros)
            folio_counter["count"] = maximo + 1
            _sb_guardar_watermark_mor(maximo)
            print(f"[INFO] Folio Morelos desde DB (primera vez): {FOLIO_NUM_PREFIJO}{maximo} "
                  f"-> siguiente: {folio_counter['count']}")
        else:
            folio_counter["count"] = 1
            print(f"[INFO] Sin folios {FOLIO_NUM_PREFIJO} previos, empezando desde {FOLIO_NUM_PREFIJO}1")
    except Exception as e:
        print(f"[ERROR] inicializar_folio Morelos: {e}")
        folio_counter["count"] = 1

def _sb_folio_existe(folio: str) -> bool:
    try:
        r = supabase.table("folios_registrados").select("folio").eq("folio", folio).execute()
        return len(r.data) > 0
    except Exception as e:
        print(f"[ERROR] Verificando folio {folio}: {e}")
        return False

def _generar_folio_sync() -> str:
    candidato = folio_counter["count"]
    for _ in range(MAX_INTENTOS_FOLIO):
        folio = f"{FOLIO_NUM_PREFIJO}{candidato}"
        if not _sb_folio_existe(folio):
            folio_counter["count"] = candidato + 1
            _sb_guardar_watermark_mor(candidato)
            print(f"[FOLIO MORELOS] Asignado: {folio} (siguiente: {folio_counter['count']})")
            return folio
        print(f"[FOLIO MORELOS] {folio} ocupado -> probando siguiente")
        candidato += 1
    import time
    fb = f"{FOLIO_NUM_PREFIJO}{int(time.time()) % 1_000_000}"
    print(f"[FOLIO MORELOS] Fallback: {fb}")
    return fb

async def generar_folio_automatico() -> str:
    async with _folio_lock:
        return await asyncio.to_thread(_generar_folio_sync)

# ── PLACA DIGITAL — WATERMARK SUPABASE ───────────────────────────────────────
# Clave en tabla folio_watermark: "MOR_PLACA"
# Codifica la placa como entero: GZR1999 → número único.
# Nunca se repite entre reinicios.

_PLACA_PREFIJO = "MOR_PLACA"
_PLACA_INICIO  = "GZR1999"
_placa_counter = {"ultimo": None}

def _placa_a_numero(placa: str) -> int:
    l1 = _ABC.index(placa[0])
    l2 = _ABC.index(placa[1])
    l3 = _ABC.index(placa[2])
    return (l1 * 676 + l2 * 26 + l3) * 10000 + int(placa[3:])

def _numero_a_placa(n: int) -> str:
    digitos = n % 10000
    idx     = n // 10000
    l3 = idx % 26
    l2 = (idx // 26) % 26
    l1 = idx // 676
    return f"{_ABC[l1]}{_ABC[l2]}{_ABC[l3]}{digitos:04d}"

def _sb_leer_watermark_placa() -> int | None:
    try:
        r = supabase.table("folio_watermark") \
            .select("ultimo_asignado").eq("prefijo", _PLACA_PREFIJO).execute()
        if r.data:
            return r.data[0]["ultimo_asignado"]
        return None
    except Exception as e:
        print(f"[ERROR] leer_watermark PLACA: {e}")
        return None

def _sb_guardar_watermark_placa(numero: int):
    try:
        supabase.table("folio_watermark").upsert({
            "prefijo":         _PLACA_PREFIJO,
            "ultimo_asignado": numero
        }).execute()
        print(f"[WATERMARK PLACA] Guardado: {_numero_a_placa(numero)}")
    except Exception as e:
        print(f"[ERROR] guardar_watermark PLACA: {e}")

def _inicializar_placa_desde_supabase():
    watermark = _sb_leer_watermark_placa()
    if watermark is not None:
        _placa_counter["ultimo"] = watermark
        print(f"[PLACA] Desde watermark Supabase: {_numero_a_placa(watermark)}")
        return
    # Fallback: archivo local
    try:
        with open("placas_digitales.txt") as f:
            ultima = f.read().strip().split("\n")[-1].strip()
        if ultima and len(ultima) == 7:
            n = _placa_a_numero(ultima)
            _placa_counter["ultimo"] = n
            _sb_guardar_watermark_placa(n)
            print(f"[PLACA] Desde archivo local (primera vez): {ultima}")
            return
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[WARN] Leyendo placas_digitales.txt: {e}")
    # Sin historial
    n = _placa_a_numero(_PLACA_INICIO)
    _placa_counter["ultimo"] = n
    _sb_guardar_watermark_placa(n)
    print(f"[PLACA] Sin historial, empezando desde {_PLACA_INICIO}")

async def generar_placa_digital() -> str:
    """Genera la siguiente placa. Async con lock — sin race conditions."""
    async with _placa_lock:
        if _placa_counter["ultimo"] is None:
            await asyncio.to_thread(_inicializar_placa_desde_supabase)
        nuevo_n = _placa_counter["ultimo"] + 1
        maximo  = _placa_a_numero("ZZZ9999")
        if nuevo_n > maximo:
            nuevo_n = _placa_a_numero("AAA0000")
        _placa_counter["ultimo"] = nuevo_n
        nueva_placa = _numero_a_placa(nuevo_n)
        await asyncio.to_thread(_sb_guardar_watermark_placa, nuevo_n)
        try:
            with open("placas_digitales.txt", "a") as f:
                f.write(nueva_placa + "\n")
        except Exception as e:
            print(f"[WARN] No se pudo guardar placa en archivo: {e}")
        print(f"[PLACA] Asignada: {nueva_placa}")
        return nueva_placa

# ── FSM ───────────────────────────────────────────────────────────────────────
class PermisoForm(StatesGroup):
    marca  = State()
    linea  = State()
    anio   = State()
    serie  = State()
    motor  = State()
    color  = State()
    tipo   = State()
    nombre = State()

# ── PDF ───────────────────────────────────────────────────────────────────────
def generar_pdf_unificado(datos: dict) -> tuple:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filename = f"{OUTPUT_DIR}/{datos['folio']}_completo.pdf"
    try:
        doc1 = fitz.open(PLANTILLA_PDF)
        pg1  = doc1[0]

        for campo in ["folio", "placa", "fecha", "vigencia", "marca", "serie",
                      "linea", "motor", "anio", "color", "tipo", "nombre"]:
            if campo in coords_morelos and campo in datos:
                x, y, s, col = coords_morelos[campo]
                pg1.insert_text((x, y), str(datos[campo]), fontsize=s, color=col)

        if len(doc1) > 1:
            pg2_inner = doc1[1]
            pg2_inner.insert_text(
                coords_morelos["fecha_hoja2"][:2],
                datos["vigencia"],
                fontsize=coords_morelos["fecha_hoja2"][2],
                color=coords_morelos["fecha_hoja2"][3]
            )

        img_qr, _ = generar_qr_dinamico_morelos(datos["folio"])
        if img_qr:
            buf = BytesIO(); img_qr.save(buf, format="PNG"); buf.seek(0)
            qr_pix = fitz.Pixmap(buf.read())
            pg1.insert_image(fitz.Rect(595, 148, 595+115, 148+115),
                             pixmap=qr_pix, overlay=True)
            print(f"[QR MORELOS] Insertado en página 1")

        doc2  = fitz.open(PLANTILLA_BUENO)
        page2 = doc2[0]
        # ── FIX FECHA: siempre hora México ──
        ahora = datetime.now(TZ_MEXICO)
        page2.insert_text((155,  245), datos["nombre"].upper(),     fontsize=18, fontname="helv")
        page2.insert_text((1045, 205), datos["folio"],               fontsize=20, fontname="helv")
        page2.insert_text((1045, 275), ahora.strftime("%d/%m/%Y"),   fontsize=20, fontname="helv")
        page2.insert_text((1045, 348), ahora.strftime("%H:%M:%S"),   fontsize=20, fontname="helv")

        doc1.insert_pdf(doc2)
        doc2.close(); doc1.save(filename); doc1.close()
        print(f"[PDF UNIFICADO MORELOS] Generado: {filename}")
        return filename, True, ""

    except Exception as e:
        error_msg = f"Error generando PDF: {str(e)}"
        print(f"[ERROR PDF] {error_msg}")
        return "", False, error_msg

# ── BACKGROUND TASK ───────────────────────────────────────────────────────────
async def _generar_y_enviar_background(chat_id: int, datos: dict, user_id: int,
                                        folio: str, nombre: str,
                                        fecha_iso: str, fecha_ven_iso: str,
                                        datos_db: dict):
    folio_final = folio
    try:
        pdf_path, ok_pdf, err_pdf = await asyncio.to_thread(generar_pdf_unificado, datos)

        if not ok_pdf:
            await bot.send_message(user_id,
                f"Error generando PDF: {err_pdf}\n\nPara generar otro permiso use /banamex")
            return

        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Validar Admin",  callback_data=f"validar_{folio_final}"),
            InlineKeyboardButton(text="Detener Timer",  callback_data=f"detener_{folio_final}")
        ]])

        await bot.send_document(
            chat_id,
            FSInputFile(pdf_path),
            caption=(
                f"PERMISO DE CIRCULACION - MORELOS\n"
                f"Folio: {folio_final}\n"
                f"Titular: {nombre}\n"
                f"Vigencia: 30 dias\n\n"
                f"Documento con 2 paginas\n"
                f"TIMER ACTIVO (36 horas)"
            ),
            reply_markup=keyboard
        )

        def _insert(folio_usar: str):
            supabase.table("folios_registrados").insert({
                "folio":             folio_usar,
                "marca":             datos_db["marca"],
                "linea":             datos_db["linea"],
                "anio":              datos_db["anio"],
                "numero_serie":      datos_db["serie"],
                "numero_motor":      datos_db["motor"],
                "color":             datos_db["color"],
                "nombre":            nombre,
                "fecha_expedicion":  fecha_iso,
                "fecha_vencimiento": fecha_ven_iso,
                "entidad":           "morelos",
                "estado":            "PENDIENTE",
                "user_id":           user_id,
                "username":          datos_db.get("username", "Sin username")
            }).execute()
            supabase.table("borradores_registros").insert({
                "folio":             folio_usar,
                "entidad":           "Morelos",
                "numero_serie":      datos_db["serie"],
                "marca":             datos_db["marca"],
                "linea":             datos_db["linea"],
                "numero_motor":      datos_db["motor"],
                "anio":              datos_db["anio"],
                "color":             datos_db["color"],
                "fecha_expedicion":  fecha_iso,
                "fecha_vencimiento": fecha_ven_iso,
                "contribuyente":     nombre,
                "estado":            "PENDIENTE",
                "user_id":           user_id
            }).execute()

        for _ in range(20):
            try:
                await asyncio.to_thread(_insert, folio_final)
                print(f"[DB] Insertado folio {folio_final}")
                break
            except Exception as e:
                em = str(e).lower()
                if any(k in em for k in ("duplicate", "unique", "23505")):
                    print(f"[DB] Folio {folio_final} duplicado — obteniendo nuevo...")
                    folio_final = await generar_folio_automatico()
                else:
                    print(f"[DB ERROR] {e}"); break

        await iniciar_timer_eliminacion(user_id, folio_final, nombre)

        await bot.send_message(user_id,
            f"INSTRUCCIONES DE PAGO\n\n"
            f"Folio: {folio_final}\n"
            f"Monto: ${PRECIO_PERMISO}\n"
            f"Tiempo limite: 36 horas\n\n"
            f"TRANSFERENCIA:\n"
            f"Banco: AZTECA\n"
            f"Titular: LIZBETH LAZCANO MOSCO\n"
            f"Cuenta: 127180013037579543\n"
            f"Concepto: Permiso {folio_final}\n\n"
            f"OXXO:\n"
            f"Referencia: 2242170180385581\n"
            f"Titular: LIZBETH LAZCANO MOSCO\n"
            f"Monto: ${PRECIO_PERMISO}\n\n"
            f"Envia la foto del comprobante para validar.\n"
            f"Si no pagas en 36 horas el folio se elimina automaticamente.\n\n"
            f"Para generar otro permiso use /banamex")

    except Exception as e:
        print(f"[ERROR] background folio {folio_final}: {e}")
        try:
            await bot.send_message(user_id,
                f"Error al generar el documento: {e}\n\nUse /banamex para reintentar.")
        except Exception:
            pass

# ── HANDLERS ──────────────────────────────────────────────────────────────────
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "SISTEMA DIGITAL DEL ESTADO DE MORELOS\n\n"
        f"Costo: ${PRECIO_PERMISO}\n"
        "Tiempo limite: 36 horas\n\n"
        "Su folio sera eliminado automaticamente si no realiza el pago dentro del tiempo limite"
    )

@dp.message(Command("banamex"))
async def banamex_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    mis_folios = [f for f in timers_activos
                  if timers_activos[f].get("user_id") == message.from_user.id]

    if mis_folios:
        texto   = "FOLIOS ACTIVOS CON TIMER\n" + "─" * 28 + "\n\n"
        botones = []
        for f in mis_folios:
            info   = timers_activos[f]
            nombre = info.get("nombre", "Sin nombre")
            mins   = max(0, 2160 - int((datetime.now() - info["start_time"]).total_seconds() / 60))
            texto += f"Folio: {f}\n{nombre}\n{mins//60}h {mins%60}min restantes\n\n"
            botones.append([InlineKeyboardButton(
                text=f"Detener timer {f}", callback_data=f"detener_{f}")])
        await message.answer(texto.strip(),
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=botones))
        await message.answer(
            f"Para NUEVO permiso escribe la MARCA del vehiculo:\n\nCosto: ${PRECIO_PERMISO} | Plazo: 36h")
    else:
        await message.answer(
            f"NUEVO PERMISO - MORELOS\n\n"
            f"Costo: ${PRECIO_PERMISO}\n"
            f"Plazo de pago: 36 horas\n\n"
            f"Primer paso: MARCA del vehiculo:")
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    await state.update_data(marca=message.text.strip().upper())
    await message.answer("LINEA/MODELO del vehiculo:")
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    await state.update_data(linea=message.text.strip().upper())
    await message.answer("ANO del vehiculo (4 digitos):")
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or len(anio) != 4:
        await message.answer("Formato invalido. Use 4 digitos (ej. 2021):")
        return
    await state.update_data(anio=anio)
    await message.answer("NUMERO DE SERIE:")
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    await state.update_data(serie=message.text.strip().upper())
    await message.answer("NUMERO DE MOTOR:")
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    await state.update_data(motor=message.text.strip().upper())
    await message.answer("COLOR del vehiculo:")
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    await state.update_data(color=message.text.strip().upper())
    await message.answer("TIPO de vehiculo:")
    await state.set_state(PermisoForm.tipo)

@dp.message(PermisoForm.tipo)
async def get_tipo(message: types.Message, state: FSMContext):
    await state.update_data(tipo=message.text.strip().upper())
    await message.answer("NOMBRE COMPLETO del propietario:")
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos  = await state.get_data()
    nombre = message.text.strip().upper()

    folio = await generar_folio_automatico()
    placa = await generar_placa_digital()          # ← async, guarda en Supabase

    # ── FIX FECHAS: siempre hora México, nunca UTC del servidor ──
    ahora         = datetime.now(TZ_MEXICO)
    vence         = ahora + timedelta(days=30)
    fecha_iso     = ahora.strftime("%Y-%m-%d")
    fecha_ven_iso = vence.strftime("%Y-%m-%d")
    fecha_texto   = ahora.strftime("%d/%m/%Y")
    vigencia_texto= vence.strftime("%d/%m/%Y")

    datos_pdf = {
        "folio":   folio,
        "placa":   placa,
        "fecha":   fecha_texto,
        "vigencia":vigencia_texto,
        "marca":   datos["marca"],
        "linea":   datos["linea"],
        "anio":    datos["anio"],
        "serie":   datos["serie"],
        "motor":   datos["motor"],
        "color":   datos["color"],
        "tipo":    datos["tipo"],
        "nombre":  nombre,
    }

    datos_db = {**datos, "username": message.from_user.username or "Sin username"}

    await state.clear()

    await message.answer(
        f"Generando documentacion...\n"
        f"Folio: {folio}\n"
        f"Titular: {nombre}"
    )

    asyncio.create_task(
        _generar_y_enviar_background(
            message.chat.id, datos_pdf, message.from_user.id,
            folio, nombre, fecha_iso, fecha_ven_iso, datos_db
        )
    )

# ── CALLBACKS ─────────────────────────────────────────────────────────────────
@dp.callback_query(lambda c: c.data and c.data.startswith("validar_"))
async def callback_validar_admin(callback: CallbackQuery):
    folio = callback.data.replace("validar_", "")
    if not folio.startswith("456"):
        await callback.answer("Folio invalido", show_alert=True); return
    if folio in timers_activos:
        uid    = timers_activos[folio]["user_id"]
        nombre = timers_activos[folio].get("nombre", "")
        cancelar_timer_folio(folio)
        try:
            now = datetime.now().isoformat()
            await asyncio.to_thread(lambda: (
                supabase.table("folios_registrados").update(
                    {"estado": "VALIDADO_ADMIN", "fecha_comprobante": now}
                ).eq("folio", folio).execute(),
                supabase.table("borradores_registros").update(
                    {"estado": "VALIDADO_ADMIN", "fecha_comprobante": now}
                ).eq("folio", folio).execute()
            ))
        except Exception as e:
            print(f"Error BD validar {folio}: {e}")
        await callback.answer("Folio validado por administracion", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        try:
            await bot.send_message(uid,
                f"PAGO VALIDADO POR ADMINISTRACION - MORELOS\n"
                f"Folio: {folio}\nTitular: {nombre}\n"
                f"Tu permiso esta activo para circular.\n\n"
                f"Para generar otro permiso use /banamex")
        except Exception as e:
            print(f"Error notificando usuario {uid}: {e}")
    else:
        await callback.answer("Folio no encontrado en timers activos", show_alert=True)

@dp.callback_query(lambda c: c.data and c.data.startswith("detener_"))
async def callback_detener_timer(callback: CallbackQuery):
    folio = callback.data.replace("detener_", "")
    if folio in timers_activos:
        nombre = timers_activos[folio].get("nombre", "")
        cancelar_timer_folio(folio)
        try:
            await asyncio.to_thread(lambda: supabase.table("folios_registrados").update(
                {"estado": "TIMER_DETENIDO", "fecha_detencion": datetime.now().isoformat()}
            ).eq("folio", folio).execute())
        except Exception as e:
            print(f"Error BD detener {folio}: {e}")
        await callback.answer("Timer detenido exitosamente", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            f"TIMER DETENIDO\nFolio: {folio}\nTitular: {nombre}\n\n"
            f"El folio ya NO se eliminara automaticamente.\n\n"
            f"Para generar otro permiso use /banamex")
    else:
        await callback.answer("Timer ya no esta activo", show_alert=True)

@dp.message(lambda m: m.text and m.text.upper().startswith("SERO") and len(m.text) > 4)
async def comando_admin_sero(message: types.Message):
    folio_admin = message.text.upper()[4:].strip()
    if not folio_admin.startswith("456"):
        await message.answer(
            f"FOLIO INVALIDO\nEl folio {folio_admin} no es MORELOS.\n"
            f"Debe comenzar con 456\n\nPara generar otro permiso use /banamex"); return
    if folio_admin in timers_activos:
        uid    = timers_activos[folio_admin]["user_id"]
        nombre = timers_activos[folio_admin].get("nombre", "")
        cancelar_timer_folio(folio_admin)
        try:
            now = datetime.now().isoformat()
            await asyncio.to_thread(lambda: (
                supabase.table("folios_registrados").update(
                    {"estado": "VALIDADO_ADMIN", "fecha_comprobante": now}
                ).eq("folio", folio_admin).execute(),
                supabase.table("borradores_registros").update(
                    {"estado": "VALIDADO_ADMIN", "fecha_comprobante": now}
                ).eq("folio", folio_admin).execute()
            ))
        except Exception as e:
            print(f"Error BD SERO {folio_admin}: {e}")
        await message.answer(
            f"VALIDACION ADMINISTRATIVA OK\nFolio: {folio_admin}\nTitular: {nombre}\n"
            f"Timer cancelado.\n\nPara generar otro permiso use /banamex")
        try:
            await bot.send_message(uid,
                f"PAGO VALIDADO POR ADMINISTRACION - MORELOS\n"
                f"Folio: {folio_admin}\nTu permiso esta activo.\n\n"
                f"Para generar otro permiso use /banamex")
        except Exception as e:
            print(f"Error notificando usuario {uid}: {e}")
    else:
        await message.answer(
            f"FOLIO NO LOCALIZADO EN TIMERS ACTIVOS\nFolio: {folio_admin}\n\n"
            f"Para generar otro permiso use /banamex")

@dp.message(lambda m: m.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    try:
        uid    = message.from_user.id
        folios = obtener_folios_usuario(uid)
        if not folios:
            await message.answer(
                "No hay tramites pendientes de pago.\n\n"
                "Para generar otro permiso use /banamex"); return
        if len(folios) > 1:
            lista = '\n'.join([f"- {f}" for f in folios])
            pending_comprobantes[uid] = "waiting_folio"
            await message.answer(
                f"Tienes varios folios activos:\n\n{lista}\n\n"
                f"Responde con el NUMERO DE FOLIO al que corresponde este comprobante.\n\n"
                f"Para generar otro permiso use /banamex"); return
        folio = folios[0]; cancelar_timer_folio(folio)
        try:
            now = datetime.now().isoformat()
            await asyncio.to_thread(lambda: (
                supabase.table("folios_registrados").update(
                    {"estado": "COMPROBANTE_ENVIADO", "fecha_comprobante": now}
                ).eq("folio", folio).execute(),
                supabase.table("borradores_registros").update(
                    {"estado": "COMPROBANTE_ENVIADO", "fecha_comprobante": now}
                ).eq("folio", folio).execute()
            ))
        except Exception as e:
            print(f"Error actualizando estado comprobante: {e}")
        await message.answer(
            f"Comprobante recibido.\nFolio: {folio}\nTimer detenido.\n\n"
            f"Para generar otro permiso use /banamex")
    except Exception as e:
        print(f"[ERROR] recibir_comprobante: {e}")
        await message.answer(
            f"Error procesando el comprobante.\n\nPara generar otro permiso use /banamex")

@dp.message(lambda message: message.from_user.id in pending_comprobantes
            and pending_comprobantes[message.from_user.id] == "waiting_folio")
async def especificar_folio_comprobante(message: types.Message):
    try:
        uid                = message.from_user.id
        folio_especificado = message.text.strip().upper()
        folios_usuario     = obtener_folios_usuario(uid)
        if folio_especificado not in folios_usuario:
            await message.answer(
                "Ese folio no esta entre tus expedientes activos.\n\n"
                "Para generar otro permiso use /banamex"); return
        cancelar_timer_folio(folio_especificado)
        del pending_comprobantes[uid]
        try:
            now = datetime.now().isoformat()
            await asyncio.to_thread(lambda: (
                supabase.table("folios_registrados").update(
                    {"estado": "COMPROBANTE_ENVIADO", "fecha_comprobante": now}
                ).eq("folio", folio_especificado).execute(),
                supabase.table("borradores_registros").update(
                    {"estado": "COMPROBANTE_ENVIADO", "fecha_comprobante": now}
                ).eq("folio", folio_especificado).execute()
            ))
        except Exception as e:
            print(f"Error actualizando estado: {e}")
        await message.answer(
            f"Comprobante asociado.\nFolio: {folio_especificado}\nTimer detenido.\n\n"
            f"Para generar otro permiso use /banamex")
    except Exception as e:
        print(f"[ERROR] especificar_folio_comprobante: {e}")
        if message.from_user.id in pending_comprobantes:
            del pending_comprobantes[message.from_user.id]
        await message.answer(
            f"Error procesando el folio.\n\nPara generar otro permiso use /banamex")

@dp.message(Command("folios"))
async def ver_folios_activos(message: types.Message):
    try:
        uid    = message.from_user.id
        folios = obtener_folios_usuario(uid)
        if not folios:
            await message.answer(
                "NO HAY FOLIOS ACTIVOS\n\n"
                "Para generar otro permiso use /banamex"); return
        lista   = []
        botones = []
        for folio in folios:
            if folio in timers_activos:
                info   = timers_activos[folio]
                nombre = info.get("nombre", "Sin nombre")
                mins   = max(0, 2160 - int(
                    (datetime.now() - info["start_time"]).total_seconds() / 60))
                lista.append(f"- {folio} — {nombre}\n  {mins//60}h {mins%60}min restantes")
            else:
                lista.append(f"- {folio} (sin timer)")
            botones.append([InlineKeyboardButton(
                text=f"Detener timer {folio}", callback_data=f"detener_{folio}")])
        await message.answer(
            f"FOLIOS MORELOS ACTIVOS ({len(folios)})\n\n" + '\n\n'.join(lista) +
            f"\n\nCada folio tiene timer de 36 horas.\n\n"
            f"Para generar otro permiso use /banamex",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=botones))
    except Exception as e:
        print(f"[ERROR] ver_folios_activos: {e}")
        await message.answer(
            f"Error consultando expedientes.\n\nPara generar otro permiso use /banamex")

@dp.message(lambda message: message.text and any(p in message.text.lower() for p in
    ['costo','precio','cuanto','cuánto','deposito','depósito','pago','valor','monto']))
async def responder_costo(message: types.Message):
    await message.answer(
        f"INFORMACION DE COSTO\n\n"
        f"El costo del permiso es ${PRECIO_PERMISO}.\n\n"
        "Para generar otro permiso use /banamex")

@dp.message()
async def fallback(message: types.Message):
    await message.answer("Sistema Digital Morelos.")

# ── FASTAPI ───────────────────────────────────────────────────────────────────
_keep_task = None

async def keep_alive():
    while True:
        await asyncio.sleep(600)
        print("[HEARTBEAT] Sistema Morelos activo")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keep_task
    inicializar_folio_desde_supabase()
    await asyncio.to_thread(_inicializar_placa_desde_supabase)   # ← placa desde Supabase
    await bot.delete_webhook(drop_pending_updates=True)
    if BASE_URL:
        wh = f"{BASE_URL}/webhook"
        await bot.set_webhook(wh, allowed_updates=["message", "callback_query"])
        print(f"[WEBHOOK] {wh}")
        _keep_task = asyncio.create_task(keep_alive())
    else:
        print("[POLLING] Sin webhook")
    placa_actual = _numero_a_placa(_placa_counter["ultimo"]) if _placa_counter["ultimo"] else "N/A"
    print(f"[SISTEMA] Morelos v6.1 listo — "
          f"siguiente folio: {FOLIO_NUM_PREFIJO}{folio_counter['count']} — "
          f"placa actual: {placa_actual}")
    yield
    if _keep_task:
        _keep_task.cancel()
        with suppress(asyncio.CancelledError): await _keep_task
    await bot.session.close()

app = FastAPI(lifespan=lifespan, title="Sistema Morelos Digital", version="6.1")

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data   = await request.json()
        update = types.Update(**data)
        await dp.feed_webhook_update(bot, update)
        return {"ok": True}
    except Exception as e:
        print(f"[ERROR] webhook: {e}")
        return {"ok": False, "error": str(e)}

@app.get("/healthz")
async def healthz():
    return {"ok": True, "service": "morelos-bot",
            "time": datetime.now(TZ_MEXICO).isoformat()}

@app.get("/consulta/{folio}")
async def consulta_folio(folio: str, request: Request):
    try:
        res = supabase.table("folios_registrados").select(
            "folio, marca, linea, anio, numero_serie, numero_motor, color, nombre, "
            "fecha_expedicion, fecha_vencimiento, estado, entidad"
        ).eq("folio", folio).execute()
        if not res.data:
            return {"ok": False, "mensaje": "Folio no encontrado"}
        return {"ok": True, "data": res.data[0]}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/")
async def root():
    placa_actual = _numero_a_placa(_placa_counter["ultimo"]) if _placa_counter["ultimo"] else "N/A"
    return {
        "ok":              True,
        "sistema":         "Morelos v6.1",
        "entidad":         "Morelos",
        "vigencia":        "30 dias",
        "timer":           "36 horas",
        "active_timers":   len(timers_activos),
        "siguiente_folio": f"{FOLIO_NUM_PREFIJO}{folio_counter['count']}",
        "placa_actual":    placa_actual,
        "cambios_v6.1": [
            "FIX fechas: datetime.now(TZ_MEXICO) — nunca UTC del servidor",
            "Placa digital en Supabase (MOR_PLACA) — nunca se repite",
        ]
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
