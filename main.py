# =========================
# CafeBotify — START v1.0 (CLIENT)
# - Меню/часы из config.json (secret file в Render)
# - Webhook (aiogram 3 + aiohttp)
# - Rate-limit: 60 сек, ставится только после подтверждения заказа
# =========================

import os
import json
import logging
import asyncio
import time
import re
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, Tuple

import redis.asyncio as redis
from aiohttp import web

from aiogram import Bot, Dispatcher, F, Router
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.client.default import DefaultBotProperties

from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application


APP_VERSION = "START v1.0 CLIENT"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

MSK_TZ = timezone(timedelta(hours=3))
RATE_LIMIT_SECONDS = 60


def _parse_work_hours(obj: Any) -> Optional[Tuple[int, int]]:
    try:
        if isinstance(obj, list) and len(obj) == 2:
            start = int(obj[0])
            end = int(obj[1])
            if 0 <= start <= 23 and 0 <= end <= 23 and start != end:
                return start, end
    except Exception:
        return None
    return None


def _read_json_file(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def load_config() -> Dict[str, Any]:
    default_config = {
        "name": "Кофейня «Уют» ☕",
        "phone": "+7 989 273-67-56",
        "admin_chat_id": 1471275603,
        "work_start": 9,
        "work_end": 21,
        "menu": {
            "☕ Капучино": 250,
            "🥛 Латте": 270,
            "🍵 Чай": 180,
            "⚡ Эспрессо": 200,
        },
    }

    # 1) Пытаемся прочитать config.json из root проекта
    data = _read_json_file("config.json")
    # 2) Если нет — пробуем secret file Render
    if data is None:
        data = _read_json_file("/etc/secrets/config.json")

    if not isinstance(data, dict):
        return default_config

    cafe = data.get("cafe", {})
    if not isinstance(cafe, dict):
        return default_config

    default_config.update(
        {
            "name": cafe.get("name", default_config["name"]),
            "phone": cafe.get("phone", default_config["phone"]),
            "admin_chat_id": cafe.get("admin_chat_id", default_config["admin_chat_id"]),
            "menu": cafe.get("menu", default_config["menu"]),
        }
    )

    wh = _parse_work_hours(cafe.get("work_hours"))
    if wh:
        default_config["work_start"], default_config["work_end"] = wh
    else:
        # Backward compatibility: work_start/work_end
        try:
            ws = cafe.get("work_start", default_config["work_start"])
            we = cafe.get("work_end", default_config["work_end"])
            ws_i, we_i = int(ws), int(we)
            if 0 <= ws_i <= 23 and 0 <= we_i <= 23 and ws_i != we_i:
                default_config["work_start"] = ws_i
                default_config["work_end"] = we_i
        except Exception:
            pass

    return default_config


cafe_config = load_config()

CAFE_NAME = cafe_config["name"]
CAFE_PHONE = cafe_config["phone"]
ADMIN_ID = int(cafe_config["admin_chat_id"])
MENU = dict(cafe_config["menu"])
WORK_START = int(cafe_config["work_start"])
WORK_END = int(cafe_config["work_end"])

BOT_TOKEN = os.getenv("BOT_TOKEN")
REDIS_URL = os.getenv("REDIS_URL")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "cafebot123")
HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME", "example.onrender.com")
PORT = int(os.getenv("PORT", 10000))

WEBHOOK_PATH = f"/{WEBHOOK_SECRET}/webhook"
WEBHOOK_URL = f"https://{HOSTNAME}{WEBHOOK_PATH}"

router = Router()


class OrderStates(StatesGroup):
    waiting_for_quantity = State()
    waiting_for_confirmation = State()


def get_moscow_time() -> datetime:
    return datetime.now(MSK_TZ)


def is_cafe_open() -> bool:
    # START v1.0: работа “в рамках дня” (без ночных смен).
    return WORK_START <= get_moscow_time().hour < WORK_END


def get_work_status() -> str:
    msk_hour = get_moscow_time().hour
    if is_cafe_open():
        remaining = max(0, WORK_END - msk_hour)
        return f"🟢 <b>Открыто</b> (ещё {remaining} ч.)"
    return f"🔴 <b>Закрыто</b>\n🕐 Открываемся: {WORK_START}:00 (МСК)"


def create_menu_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [[KeyboardButton(text=drink)] for drink in MENU.keys()]
    keyboard.append([KeyboardButton(text="📞 Позвонить"), KeyboardButton(text="⏰ Часы работы")])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def create_info_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📞 Позвонить"), KeyboardButton(text="⏰ Часы работы")]],
        resize_keyboard=True,
    )


def create_quantity_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="1️⃣"), KeyboardButton(text="2️⃣"), KeyboardButton(text="3️⃣")],
            [KeyboardButton(text="4️⃣"), KeyboardButton(text="5️⃣"), KeyboardButton(text="🔙 Отмена")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def create_confirm_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Подтвердить"), KeyboardButton(text="Меню")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def get_closed_message() -> str:
    menu_text = " • ".join([f"<b>{drink}</b> {price}₽" for drink, price in MENU.items()])
    return (
        f"🔒 <b>{CAFE_NAME} сейчас закрыто!</b>\n\n"
        f"⏰ {get_work_status()}\n\n"
        f"☕ <b>Наше меню:</b>\n{menu_text}\n\n"
        f"📞 <b>Связаться:</b>\n<code>{CAFE_PHONE}</code>\n\n"
        f"✨ <i>До скорой встречи!</i>"
    )


def get_user_name(message: Message) -> str:
    if message.from_user is None:
        return "друг"
    return message.from_user.first_name or "друг"


async def get_redis_client():
    client = redis.from_url(REDIS_URL)
    try:
        await client.ping()
        return client
    except Exception:
        await client.aclose()
        raise


def _rate_limit_key(user_id: int) -> str:
    return f"rate_limit:{user_id}"


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    msk_time = get_moscow_time().strftime("%H:%M")
    logger.info(f"👤 /start от {user_id} 
