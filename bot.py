import asyncio
import json
import logging
import math
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from typing import Iterator, Literal

import pytz
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ForceReply
from aiogram.filters import CommandStart, Command

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN. Добавьте токен бота в .env или переменные окружения.")

DATABASE_PATH = os.getenv("DATABASE_PATH", "miron_tracker.db")
TIMEZONE = pytz.timezone(os.getenv("TIMEZONE", "Europe/Tallinn"))
ML_INPUT_TIMEOUT_SECONDS = 5 * 60
ActivityType = Literal["sleep", "walk"]
ACTIVE_ACTIVITY_TYPES: tuple[ActivityType, ...] = ("sleep", "walk")


def parse_user_ids(env_name: str) -> set[int]:
    value = os.getenv(env_name, "")
    try:
        return {int(item.strip()) for item in value.split(",") if item.strip()}
    except ValueError as exc:
        raise RuntimeError(f"{env_name} должен содержать Telegram ID через запятую.") from exc


ALLOWED_USER_IDS = parse_user_ids("ALLOWED_USER_IDS")
ADMIN_USER_IDS = parse_user_ids("ADMIN_USER_IDS")
if not ALLOWED_USER_IDS and not ADMIN_USER_IDS:
    raise RuntimeError("Укажите доверенные Telegram ID в ALLOWED_USER_IDS в файле .env.")
ALLOWED_USER_IDS.update(ADMIN_USER_IDS)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

def get_now_timestamp() -> float:
    return datetime.now(TIMEZONE).timestamp()


def timestamp_to_str(ts: float, fmt="%H:%M") -> str:
    return datetime.fromtimestamp(ts, TIMEZONE).strftime(fmt)


def parse_stored_time(value: str) -> float:
    if not value:
        raise ValueError("Пустое значение времени")
    try:
        timestamp = float(value)
    except ValueError:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = TIMEZONE.localize(dt)
        timestamp = dt.timestamp()
    if not math.isfinite(timestamp):
        raise ValueError("Некорректная временная метка")
    return timestamp


def today_bounds(now: datetime | None = None) -> tuple[float, float]:
    today = (now or datetime.now(TIMEZONE)).date()
    start = TIMEZONE.localize(datetime.combine(today, time.min))
    tomorrow = TIMEZONE.localize(datetime.combine(today + timedelta(days=1), time.min))
    return start.timestamp(), tomorrow.timestamp()


def parse_milliliters(text: str) -> int | None:
    match = re.fullmatch(r"\s*(\d{1,3})(?:\s*(?:мл|ml))?\s*", text, re.IGNORECASE)
    if not match:
        return None
    amount = int(match.group(1))
    return amount if 10 <= amount <= 500 else None


@contextmanager
def database_connection() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with database_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                sub_type TEXT,
                start_time REAL NOT NULL,
                end_time REAL,
                details TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

def get_current_state(key: str):
    with database_connection() as conn:
        row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None

def set_current_state(key: str, value: str):
    with database_connection() as conn:
        conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (key, value))

def delete_current_state(key: str):
    with database_connection() as conn:
        conn.execute("DELETE FROM state WHERE key = ?", (key,))


def start_activity(activity_type: ActivityType, start_time: float) -> ActivityType | None:
    if activity_type not in ACTIVE_ACTIVITY_TYPES:
        raise ValueError(f"Неизвестный тип активности: {activity_type}")

    with database_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for active_type in ACTIVE_ACTIVITY_TYPES:
            row = conn.execute(
                "SELECT 1 FROM state WHERE key = ?",
                (f"{active_type}_start",),
            ).fetchone()
            if row:
                return active_type
        conn.execute(
            "INSERT INTO state (key, value) VALUES (?, ?)",
            (f"{activity_type}_start", str(start_time)),
        )
    return None


def finish_activity(
    activity_type: ActivityType,
    end_time: float,
) -> tuple[float, int] | None:
    if activity_type not in ACTIVE_ACTIVITY_TYPES:
        raise ValueError(f"Неизвестный тип активности: {activity_type}")

    state_key = f"{activity_type}_start"
    with database_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT value FROM state WHERE key = ?",
            (state_key,),
        ).fetchone()
        if row is None:
            return None

        start_time = parse_stored_time(row[0])
        duration_minutes = int(max(0, (end_time - start_time) // 60))
        conn.execute(
            """
            INSERT INTO events (event_type, start_time, end_time, details)
            VALUES (?, ?, ?, ?)
            """,
            (activity_type, start_time, end_time, str(duration_minutes)),
        )
        conn.execute("DELETE FROM state WHERE key = ?", (state_key,))
    return start_time, duration_minutes


def waiting_key(chat_id: int) -> str:
    return f"waiting_for_ml:{chat_id}"


def set_waiting_for_ml(chat_id: int, user_id: int):
    set_current_state(
        waiting_key(chat_id),
        json.dumps({"user_id": user_id, "requested_at": get_now_timestamp()}),
    )


def get_waiting_user_id(chat_id: int) -> str | None:
    key = waiting_key(chat_id)
    value = get_current_state(key)
    if value is None:
        return None
    try:
        request = json.loads(value)
        requested_at = float(request["requested_at"])
        user_id = str(int(request["user_id"]))
        age = get_now_timestamp() - requested_at
        if not math.isfinite(requested_at) or age < 0 or age > ML_INPUT_TIMEOUT_SECONDS:
            delete_current_state(key)
            return None
        return user_id
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        logger.warning("Удален некорректный запрос ввода кормления для чата %s", chat_id)
        delete_current_state(key)
        return None


def clear_waiting_for_ml(chat_id: int):
    delete_current_state(waiting_key(chat_id))
    legacy_chat_id = get_current_state("waiting_for_ml")
    if legacy_chat_id == str(chat_id):
        delete_current_state("waiting_for_ml")

def log_event(event_type: str, sub_type: str = None, start_time: float = None, end_time: float = None, details: str = None):
    if start_time is None:
        start_time = get_now_timestamp()
    with database_connection() as conn:
        conn.execute("""
            INSERT INTO events (event_type, sub_type, start_time, end_time, details)
            VALUES (?, ?, ?, ?, ?)
        """, (event_type, sub_type, start_time, end_time, details))

def clear_today_events_in_db():
    """Удаляет сегодняшние события и обрезает до полуночи события, начавшиеся накануне."""
    today_start, tomorrow_start = today_bounds()
    with database_connection() as conn:
        rows = conn.execute(
            "SELECT id, event_type, start_time, end_time, details FROM events"
        ).fetchall()
        ids_to_delete = []
        updates = []
        for event_id, event_type, start_value, end_value, details in rows:
            try:
                start_ts = parse_stored_time(str(start_value))
                if today_start <= start_ts < tomorrow_start:
                    ids_to_delete.append((event_id,))
                elif start_ts < today_start and end_value is not None:
                    end_ts = parse_stored_time(str(end_value))
                    if end_ts > today_start:
                        prior_day_details = details
                        if event_type in {"sleep", "walk"}:
                            prior_day_details = str(max(0, int((today_start - start_ts) // 60)))
                        updates.append((today_start, prior_day_details, event_id))
            except (TypeError, ValueError, OverflowError):
                logger.warning("Пропущено событие %s с некорректным временем", event_id)
        if ids_to_delete:
            conn.executemany("DELETE FROM events WHERE id = ?", ids_to_delete)
        if updates:
            conn.executemany(
                "UPDATE events SET end_time = ?, details = ? WHERE id = ?",
                updates,
            )

def get_today_summary():
    now_ts = get_now_timestamp()
    today_start, tomorrow_start = today_bounds()
    with database_connection() as conn:
        all_rows = conn.execute(
            "SELECT event_type, sub_type, start_time, end_time, details FROM events ORDER BY id ASC"
        ).fetchall()

    today_rows = []
    for event_type, sub_type, start_value, end_value, details in all_rows:
        try:
            start_ts = parse_stored_time(str(start_value))
            end_ts = parse_stored_time(str(end_value)) if end_value is not None else None
            is_interval = event_type in {"sleep", "walk"}
            overlaps_today = (
                start_ts < tomorrow_start and (end_ts is None or end_ts > today_start)
                if is_interval
                else today_start <= start_ts < tomorrow_start
            )
            if overlaps_today:
                today_rows.append((event_type, sub_type, start_ts, end_ts, details))
        except (TypeError, ValueError, OverflowError):
            logger.warning("Пропущено событие с некорректным временем: %r", start_value)

    for event_type, state_key in (("sleep", "sleep_start"), ("walk", "walk_start")):
        active_start = get_current_state(state_key)
        if active_start:
            try:
                start_ts = parse_stored_time(active_start)
                if start_ts < tomorrow_start and now_ts > today_start:
                    today_rows.append((event_type, None, start_ts, None, None))
            except (TypeError, ValueError, OverflowError):
                logger.warning("Некорректное время активного статуса %s", state_key)

    return sorted(today_rows, key=lambda row: row[2])


def format_today_summary(events, now_ts: float | None = None) -> str | None:
    if not events:
        return None

    now_ts = now_ts if now_ts is not None else get_now_timestamp()
    today_start, _ = today_bounds()
    total_sleep = 0
    total_walk = 0
    total_bottle = 0
    diapers_count = 0
    events_list_text = ""

    for event_type, sub_type, start_time, end_time, details in events:
        dt = timestamp_to_str(start_time, "%d.%m %H:%M") if start_time < today_start else timestamp_to_str(start_time)

        if event_type == "sleep":
            segment_start = max(start_time, today_start)
            segment_end = min(end_time if end_time is not None else now_ts, now_ts)
            mins = max(0, int((segment_end - segment_start) // 60))
            total_sleep += mins
            end_label = timestamp_to_str(end_time) if end_time is not None else "сейчас"
            ongoing = " (продолжается)" if end_time is None else ""
            events_list_text += f"• 💤 [{dt}–{end_label}] Сон: {mins // 60}ч {mins % 60}мин{ongoing}\n"
        elif event_type == "walk":
            segment_start = max(start_time, today_start)
            segment_end = min(end_time if end_time is not None else now_ts, now_ts)
            mins = max(0, int((segment_end - segment_start) // 60))
            total_walk += mins
            end_label = timestamp_to_str(end_time) if end_time is not None else "сейчас"
            ongoing = " (продолжается)" if end_time is None else ""
            events_list_text += f"• 🌳 [{dt}–{end_label}] Прогулка: {mins // 60}ч {mins % 60}мин{ongoing}\n"
        elif event_type == "feeding":
            events_list_text += f"• 🍼 [{dt}] Бутылочка: {details} мл\n"
            try:
                total_bottle += int(details)
            except (TypeError, ValueError):
                logger.warning("Пропущен объем кормления в отчете: %r", details)
        elif event_type == "diaper":
            events_list_text += f"• 🧷 [{dt}] Подгузник: {sub_type}\n"
            diapers_count += 1

    return (
        "<b>📊 Сводка для Мирона за сегодня:</b>\n\n"
        f"💤 Всего сна: <b>{total_sleep // 60}ч {total_sleep % 60}мин</b>\n"
        f"🌳 Всего прогулок: <b>{total_walk // 60}ч {total_walk % 60}мин</b>\n"
        f"🍼 Из бутылочки: <b>{total_bottle} мл</b>\n"
        f"🧷 Смен подгузника: <b>{diapers_count}</b>\n\n"
        "<b>📝 Хронология:</b>\n"
        f"{events_list_text}"
    )


# =====================================================================
# КЛАВИАТУРЫ
# =====================================================================
def get_main_keyboard():
    is_sleeping = get_current_state("sleep_start")
    is_walking = get_current_state("walk_start")

    sleep_btn = KeyboardButton(text="⏰ Проснулся") if is_sleeping else KeyboardButton(text="💤 Уложить спать")
    walk_btn = KeyboardButton(text="🏠 Вернулись с прогулки") if is_walking else KeyboardButton(text="🌳 Пошли гулять")

    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [sleep_btn, walk_btn],
            [KeyboardButton(text="🍼 Бутылочка"), KeyboardButton(text="🧷 Подгузник")],
            [KeyboardButton(text="📊 Отчет за сегодня"), KeyboardButton(text="🔄 Обновить статус")]
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие..."
    )
    return keyboard

def get_bottle_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="30 мл"), KeyboardButton(text="60 мл"), KeyboardButton(text="90 мл")],
            [KeyboardButton(text="120 мл"), KeyboardButton(text="150 мл"), KeyboardButton(text="180 мл")],
            [KeyboardButton(text="✏️ Свое количество")],
            [KeyboardButton(text="« Назад")]
        ],
        resize_keyboard=True
    )

def get_diaper_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💧 Мокрый"), KeyboardButton(text="💩 Грязный")],
            [KeyboardButton(text="✨ И то, и другое")],
            [KeyboardButton(text="« Назад")]
        ],
        resize_keyboard=True
    )

# =====================================================================
# ФОРМИРОВАНИЕ ДЭШБОРДА
# =====================================================================
def build_dashboard_text():
    now_ts = get_now_timestamp()
    sleep_start = get_current_state("sleep_start")
    walk_start = get_current_state("walk_start")

    status_text = "<b>👶 Статус Мирона:</b>\n"

    if sleep_start:
        try:
            start_ts = parse_stored_time(sleep_start)
            duration_seconds = int(now_ts - start_ts)
            minutes = max(0, duration_seconds // 60)
            status_text += f"💤 Спит уже: <b>{minutes // 60}ч {minutes % 60}мин</b> (лёг в {timestamp_to_str(start_ts)})\n"
        except (TypeError, ValueError, OverflowError):
            logger.warning("Некорректное время начала сна")
            status_text += "💤 Спит (ошибка расчета времени)\n"
    elif walk_start:
        try:
            start_ts = parse_stored_time(walk_start)
            duration_seconds = int(now_ts - start_ts)
            minutes = max(0, duration_seconds // 60)
            status_text += f"🌳 Гуляет уже: <b>{minutes // 60}ч {minutes % 60}мин</b> (вышли в {timestamp_to_str(start_ts)})\n"
        except (TypeError, ValueError, OverflowError):
            logger.warning("Некорректное время начала прогулки")
            status_text += "🌳 Гуляет (ошибка расчета времени)\n"
    else:
        status_text += "🧸 Активен (бодрствует дома)\n"

    with database_connection() as conn:
        last_feed = conn.execute(
            "SELECT start_time, details FROM events WHERE event_type='feeding' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    if last_feed:
        try:
            feed_ts = parse_stored_time(str(last_feed[0]))
            status_text += f"🍼 Последнее кормление: <b>{timestamp_to_str(feed_ts)}</b> ({last_feed[1]} мл)\n"
        except (TypeError, ValueError, OverflowError):
            logger.warning("Некорректное время последнего кормления")
            status_text += f"🍼 Последнее кормление: данные повреждены\n"
    else:
        status_text += "🍼 Последнее кормление: нет данных\n"

    status_text += f"\n🕒 <i>Данные на: {timestamp_to_str(now_ts, '%H:%M:%S')}</i>"
    return status_text

# =====================================================================
# ХЕНДЛЕРЫ БОТА
# =====================================================================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


async def restrict_to_allowed_users(handler, event, data):
    user = event.from_user
    if ALLOWED_USER_IDS and (user is None or user.id not in ALLOWED_USER_IDS):
        if isinstance(event, Message):
            await event.answer("⛔ У вас нет доступа к этому боту.")
        return None
    return await handler(event, data)


dp.message.outer_middleware(restrict_to_allowed_users)

@dp.message(CommandStart())
async def cmd_start(message: Message):
    clear_waiting_for_ml(message.chat.id)
    await message.answer(text=build_dashboard_text(), reply_markup=get_main_keyboard(), parse_mode="HTML")

@dp.message(Command("reset_today"))
async def cmd_reset_today(message: Message):
    if message.from_user is None or message.from_user.id not in ADMIN_USER_IDS:
        await message.answer("⛔ Сброс доступен только администраторам, указанным в ADMIN_USER_IDS.")
        return
    clear_waiting_for_ml(message.chat.id)
    clear_today_events_in_db()
    await message.answer(
        "🗑 <b>События за сегодня удалены.</b> История прошлых дней и текущий сон/прогулка сохранены.",
        parse_mode="HTML"
    )
    await message.answer(text=build_dashboard_text(), reply_markup=get_main_keyboard(), parse_mode="HTML")

@dp.message(F.text == "« Назад")
@dp.message(F.text == "🔄 Обновить статус")
async def cmd_refresh(message: Message):
    clear_waiting_for_ml(message.chat.id)
    await message.answer(text=build_dashboard_text(), reply_markup=get_main_keyboard(), parse_mode="HTML")

# --- СНОВИДЕНИЯ ---
@dp.message(F.text == "💤 Уложить спать")
async def cmd_go_to_sleep(message: Message):
    clear_waiting_for_ml(message.chat.id)
    now_ts = get_now_timestamp()
    active_activity = start_activity("sleep", now_ts)
    if active_activity is not None:
        if active_activity == "sleep":
            await message.answer("Мирон уже спит. Нажмите «⏰ Проснулся», чтобы завершить сон.")
        else:
            await message.answer("Сначала завершите прогулку кнопкой «🏠 Вернулись с прогулки».")
        return
    await message.answer(f"🌙 <b>{timestamp_to_str(now_ts)}</b> — Мирон уснул.", parse_mode="HTML")
    await message.answer(text=build_dashboard_text(), reply_markup=get_main_keyboard(), parse_mode="HTML")

@dp.message(F.text == "⏰ Проснулся")
async def cmd_wake_up(message: Message):
    clear_waiting_for_ml(message.chat.id)
    now_ts = get_now_timestamp()
    try:
        finished_activity = finish_activity("sleep", now_ts)
        if finished_activity is not None:
            _, duration_mins = finished_activity
            await message.answer(text=f"☀️ <b>{timestamp_to_str(now_ts)}</b> — Мирон проснулся. Поспал: <b>{duration_mins // 60}ч {duration_mins % 60}мин</b>.", parse_mode="HTML")
        else:
            await message.answer("Ошибка: Старт сна не найден в базе.")
    except (TypeError, ValueError, OverflowError):
        logger.exception("Не удалось завершить сон")
        await message.answer("Не удалось обработать время сна. Статус сохранен; попробуйте обновить его позже.")
    await message.answer(text=build_dashboard_text(), reply_markup=get_main_keyboard(), parse_mode="HTML")

# --- ПРОГУЛКА ---
@dp.message(F.text == "🌳 Пошли гулять")
async def cmd_walk_start(message: Message):
    clear_waiting_for_ml(message.chat.id)
    now_ts = get_now_timestamp()
    active_activity = start_activity("walk", now_ts)
    if active_activity is not None:
        if active_activity == "walk":
            await message.answer("Мирон уже гуляет. Нажмите «🏠 Вернулись с прогулки», чтобы завершить прогулку.")
        else:
            await message.answer("Сначала завершите сон кнопкой «⏰ Проснулся».")
        return
    await message.answer(f"🌳 <b>{timestamp_to_str(now_ts)}</b> — Пошли гулять.", parse_mode="HTML")
    await message.answer(text=build_dashboard_text(), reply_markup=get_main_keyboard(), parse_mode="HTML")

@dp.message(F.text == "🏠 Вернулись с прогулки")
async def cmd_walk_end(message: Message):
    clear_waiting_for_ml(message.chat.id)
    now_ts = get_now_timestamp()
    try:
        finished_activity = finish_activity("walk", now_ts)
        if finished_activity is not None:
            _, duration_mins = finished_activity
            await message.answer(text=f"🏠 <b>{timestamp_to_str(now_ts)}</b> — Вернулись домой. Гуляли: <b>{duration_mins // 60}ч {duration_mins % 60}мин</b>.", parse_mode="HTML")
        else:
            await message.answer("Ошибка: Время начала прогулки не найдено.")
    except (TypeError, ValueError, OverflowError):
        logger.exception("Не удалось завершить прогулку")
        await message.answer("Не удалось обработать время прогулки. Статус сохранен; попробуйте обновить его позже.")
    await message.answer(text=build_dashboard_text(), reply_markup=get_main_keyboard(), parse_mode="HTML")

# --- ПОДГУЗНИК ---
@dp.message(F.text == "🧷 Подгузник")
async def cmd_diaper_menu(message: Message):
    clear_waiting_for_ml(message.chat.id)
    await message.answer("🧷 Состояние подгузника:", reply_markup=get_diaper_keyboard())

@dp.message(F.text.in_({"💧 Мокрый", "💩 Грязный", "✨ И то, и другое"}))
async def cmd_diaper_select(message: Message):
    clear_waiting_for_ml(message.chat.id)
    status = message.text
    now_ts = get_now_timestamp()
    log_event(event_type="diaper", sub_type=status, start_time=now_ts)
    await message.answer(f"🧷 <b>{timestamp_to_str(now_ts)}</b> — Смена подгузника ({status}).", parse_mode="HTML")
    await message.answer(text=build_dashboard_text(), reply_markup=get_main_keyboard(), parse_mode="HTML")

# --- КОРМЛЕНИЕ (ОСНОВНЫЕ КНОПКИ) ---
@dp.message(F.text == "🍼 Бутылочка")
async def cmd_bottle_menu(message: Message):
    clear_waiting_for_ml(message.chat.id)
    await message.answer("🍼 Выберите объем смеси или нажмите «Свое количество»:", reply_markup=get_bottle_keyboard())

@dp.message(F.text == "✏️ Свое количество")
async def cmd_custom_bottle(message: Message):
    set_waiting_for_ml(message.chat.id, message.from_user.id)
    await message.answer(
        "✍️ В течение 5 минут отправьте количество от 10 до 500 мл. Ожидаю только ваш ответ в этом чате:",
        reply_markup=ForceReply(selective=True)
    )

@dp.message(F.text.in_({"30 мл", "60 мл", "90 мл", "120 мл", "150 мл", "180 мл"}))
async def cmd_feed_bottle(message: Message):
    clear_waiting_for_ml(message.chat.id)
    ml = message.text.split()[0]
    now_ts = get_now_timestamp()
    log_event(event_type="feeding", sub_type="Бутылочка", start_time=now_ts, details=ml)
    await message.answer(f"🍼 <b>{timestamp_to_str(now_ts)}</b> — Мирон съел <b>{ml} мл</b>.", parse_mode="HTML")
    await message.answer(text=build_dashboard_text(), reply_markup=get_main_keyboard(), parse_mode="HTML")

# --- ОТЧЕТ ---
@dp.message(F.text == "📊 Отчет за сегодня")
async def cmd_summary(message: Message):
    clear_waiting_for_ml(message.chat.id)
    text = format_today_summary(get_today_summary())
    if text is None:
        await message.answer("Событий за сегодня еще нет.")
        return
    await message.answer(text=text, reply_markup=get_main_keyboard(), parse_mode="HTML")

# --- УМНЫЙ ПЕРЕХВАТЧИК ТЕКСТА ---
@dp.message(F.text)
async def process_any_text(message: Message):
    text = message.text.strip().lower()
    pending_user_id = get_waiting_user_id(message.chat.id)
    if pending_user_id is not None:
        if message.from_user is None or str(message.from_user.id) != pending_user_id:
            return
        ml = parse_milliliters(text)
        if ml is None:
            await message.answer("Введите количество от 10 до 500 мл (например, 120).")
            return
        clear_waiting_for_ml(message.chat.id)
        now_ts = get_now_timestamp()
        log_event(event_type="feeding", sub_type="Бутылочка", start_time=now_ts, details=str(ml))
        await message.answer(f"🍼 <b>{timestamp_to_str(now_ts)}</b> — Мирон съел <b>{ml} мл</b>.", parse_mode="HTML")
        await message.answer(text=build_dashboard_text(), reply_markup=get_main_keyboard(), parse_mode="HTML")
        return

    if re.fullmatch(r"\d+\s*(?:мл|ml)", text, re.IGNORECASE):
        ml = parse_milliliters(text)
        if ml is None:
            await message.answer("Объем кормления должен быть от 10 до 500 мл.")
            return
        now_ts = get_now_timestamp()
        log_event(event_type="feeding", sub_type="Бутылочка", start_time=now_ts, details=str(ml))
        await message.answer(f"🍼 <b>{timestamp_to_str(now_ts)}</b> — Мирон съел <b>{ml} мл</b>.", parse_mode="HTML")
        await message.answer(text=build_dashboard_text(), reply_markup=get_main_keyboard(), parse_mode="HTML")

# =====================================================================
# ЗАПУСК
# =====================================================================
async def main():
    init_db()
    logging.info("База данных готова. Запуск бота...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
