import os
import json
import logging
import asyncio
import time
import random
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, Tuple

import redis.asyncio as redis
from aiohttp import web

from aiogram import Bot, Dispatcher, F, Router
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    BotCommand,            # <-- добавлено
)
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.client.default import DefaultBotProperties
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

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


def load_config() -> Dict[str, Any]:
    """
    Новый формат:
    {
      "cafes": [
        {
          "id": "cafe_uyut",
          "name": "...",
          "phone": "...",
          "admin_chat_id": 111,
          "work_start": 9,
          "work_end": 21,
          "menu": {...}
        }
      ]
    }
    """
    default_cafe = {
        "id": "default_cafe",
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

    try:
        with open("config.json", "r", encoding="utf-8") as f:
            data = json.load(f)
            cafes = data.get("cafes", [])
            if cafes and isinstance(cafes, list):
                cafe = cafes[0]  # пока работаем с первым кафе
            else:
                cafe = data.get("cafe", {})

            default_cafe.update(
                {
                    "id": cafe.get("id", default_cafe["id"]),
                    "name": cafe.get("name", default_cafe["name"]),
                    "phone": cafe.get("phone", default_cafe["phone"]),
                    "admin_chat_id": cafe.get("admin_chat_id", default_cafe["admin_chat_id"]),
                    "menu": cafe.get("menu", default_cafe["menu"]),
                }
            )

            # поддержка обоих вариантов: work_hours: [9,21] или work_start/work_end
            wh = _parse_work_hours(cafe.get("work_hours"))
            if wh:
                default_cafe["work_start"], default_cafe["work_end"] = wh
            else:
                try:
                    ws = cafe.get("work_start", default_cafe["work_start"])
                    we = cafe.get("work_end", default_cafe["work_end"])
                    ws_i, we_i = int(ws), int(we)
                    if 0 <= ws_i <= 23 and 0 <= we_i <= 23 and ws_i != we_i:
                        default_cafe["work_start"] = ws_i
                        default_cafe["work_end"] = we_i
                except Exception:
                    pass
    except Exception:
        # останемся на дефолте
        pass

    return default_cafe


cafe_config = load_config()

CAFE_ID = cafe_config["id"]
CAFE_NAME = cafe_config["name"]
CAFE_PHONE = cafe_config["phone"]
ADMIN_ID = int(cafe_config["admin_chat_id"])
MENU = dict(cafe_config["menu"])

WORK_START = int(cafe_config["work_start"])
WORK_END = int(cafe_config["work_end"])

BOT_TOKEN = os.getenv("BOT_TOKEN")
REDIS_URL = os.getenv("REDIS_URL")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "cafebot123")
HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME", "chatbotify-2tjd.onrender.com")
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
    return WORK_START <= get_moscow_time().hour < WORK_END


def get_work_status() -> str:
    msk_hour = get_moscow_time().hour
    if is_cafe_open():
        remaining = max(0, WORK_END - msk_hour)
        return f"🟢 <b>Открыто</b> (ещё {remaining} ч.)"
    return f"🔴 <b>Закрыто</b>\n🕐 Открываемся: {WORK_START}:00 (МСК)"


# ---------- клавиатуры ----------

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


# ---------- тёплые тексты ----------

WELCOME_VARIANTS = [
    "Рад тебя видеть, {name}! Сегодня что-то классическое или попробуем новинку?",
    "{name}, добро пожаловать! Я уже грею молоко — выбирай, что приготовить.",
    "Заходи, {name}! Сейчас самое время для вкусного перерыва.",
    "{name}, привет! Устроим небольшой кофейный ритуал?",
    "Отлично, что заглянул, {name}! Давай подберём идеальный напиток под настроение.",
]

CHOICE_VARIANTS = [
    "Отличный выбор! Такое сейчас особенно популярно.",
    "Классика, которая никогда не подводит.",
    "Мне тоже нравится этот вариант — не прогадаешь.",
    "Прекрасный вкус, {name}! Это один из хитов нашего меню.",
    "Вот это да, {name}! Любители хорошего кофе тебя поймут.",
    "Смело! Такой выбор обычно делают настоящие ценители.",
    "{name}, ты знаешь толк в напитках.",
    "Звучит вкусно — уже представляю аромат.",
]

FINISH_VARIANTS = [
    "Спасибо за заказ! Буду рад увидеть тебя снова.",
    "Рад был помочь с выбором. Заглядывай ещё — всегда ждём.",
    "Отличный заказ! Надеюсь, это сделает день чуточку лучше.",
    "Спасибо, что выбрал именно нас. До следующей кофейной паузы!",
    "Заказ готовим с заботой. Возвращайся, когда захочется повторить.",
]


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


# -------------------------
# Пользовательский флоу
# -------------------------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    name = get_user_name(message)
    msk_time = get_moscow_time().strftime("%H:%M")
    logger.info(f"👤 /start от {user_id} | MSK: {msk_time} | cafe={CAFE_ID}")

    welcome = random.choice(WELCOME_VARIANTS).format(name=name)

    if is_cafe_open():
        await message.answer(
            f"{welcome}\n\n"
            f"🕐 <i>Московское время: {msk_time}</i>\n"
            f"🏪 {get_work_status()}\n\n"
            f"☕ <b>Выберите напиток:</b>",
            reply_markup=create_menu_keyboard(),
        )
    else:
        await message.answer(get_closed_message(), reply_markup=create_info_keyboard())


@router.message(F.text.in_(set(MENU.keys())))
async def drink_selected(message: Message, state: FSMContext):
    user_id = message.from_user.id
    name = get_user_name(message)
    logger.info(f"🥤 {message.text} от {user_id} | cafe={CAFE_ID}")

    if not is_cafe_open():
        await message.answer(get_closed_message(), reply_markup=create_info_keyboard())
        return

    drink = message.text
    price = MENU[drink]

    await state.set_state(OrderStates.waiting_for_quantity)
    await state.set_data({"drink": drink, "price": price})

    choice_text = random.choice(CHOICE_VARIANTS).format(name=name)

    await message.answer(
        f"{choice_text}\n\n"
        f"🥤 <b>{drink}</b>\n💰 <b>{price} ₽</b>\n\n📝 <b>Сколько порций?</b>",
        reply_markup=create_quantity_keyboard(),
    )


@router.message(StateFilter(OrderStates.waiting_for_quantity))
async def process_quantity(message: Message, state: FSMContext):
    if message.text == "🔙 Отмена":
        await state.clear()
        await message.answer(
            "❌ Заказ отменён",
            reply_markup=create_menu_keyboard() if is_cafe_open() else create_info_keyboard(),
        )
        return

    try:
        quantity = int(message.text[0])
        if 1 <= quantity <= 5:
            data = await state.get_data()
            drink, price = data["drink"], data["price"]
            total = price * quantity

            await state.set_state(OrderStates.waiting_for_confirmation)
            await state.update_data(quantity=quantity, total=total)

            await message.answer(
                f"🥤 <b>{drink}</b> × {quantity}\n💰 Итого: <b>{total} ₽</b>\n\n✅ Правильно?",
                reply_markup=create_confirm_keyboard(),
            )
        else:
            await message.answer("❌ Выберите от 1 до 5", reply_markup=create_quantity_keyboard())
    except ValueError:
        await message.answer("❌ Нажмите на кнопку", reply_markup=create_quantity_keyboard())


@router.message(StateFilter(OrderStates.waiting_for_confirmation))
async def process_confirmation(message: Message, state: FSMContext):
    if message.text == "Подтвердить":
        # Rate-limit только после подтверждённого заказа
        try:
            r_client = await get_redis_client()
            user_id = message.from_user.id
            last_order = await r_client.get(_rate_limit_key(user_id))
            if last_order and time.time() - float(last_order) < RATE_LIMIT_SECONDS:
                await message.answer(
                    f"⏳ Дай мне минутку: новый заказ можно оформить через {RATE_LIMIT_SECONDS} секунд после предыдущего.",
                    reply_markup=create_menu_keyboard(),
                )
                await r_client.aclose()
                return

            await r_client.setex(_rate_limit_key(user_id), RATE_LIMIT_SECONDS, time.time())
            await r_client.aclose()
        except Exception:
            pass

        data = await state.get_data()
        drink, quantity, total = data["drink"], data["quantity"], data["total"]
        order_id = f"order:{int(time.time())}:{message.from_user.id}"
        order_num = order_id.split(":")[-1]

        user_name = message.from_user.username or message.from_user.first_name or "Клиент"
        user_id = message.from_user.id

        # Сохраняем заказ в Redis (stats + детали)
        try:
            r_client = await get_redis_client()
            await r_client.hset(
                order_id,
                mapping={
                    "user_id": user_id,
                    "username": user_name,
                    "drink": drink,
                    "quantity": quantity,
                    "total": total,
                    "timestamp": datetime.now().isoformat(),
                    "cafe_id": CAFE_ID,
                },
            )
            await r_client.expire(order_id, 86400)
            await r_client.incr(f"stats:{CAFE_ID}:total_orders")
            await r_client.incr(f"stats:{CAFE_ID}:drink:{drink}")
            await r_client.aclose()
        except Exception:
            pass

        # Ссылка, открывающая личный чат с клиентом
        user_link = f'<a href="tg://user?id={user_id}">{user_name}</a>'

        admin_message = (
            f"🔔 <b>НОВЫЙ ЗАКАЗ #{order_num}</b> | {CAFE_NAME}\n\n"
            f"{user_link}\n"
            f"<code>{user_id}</code>\n\n"
            f"{drink}\n"
            f"{quantity} порций\n"
            f"<b>{total} ₽</b>\n\n"
            f"Нажми на имя, чтобы открыть чат и ответить клиенту."
        )

        await message.bot.send_message(
            ADMIN_ID,
            admin_message,
            disable_web_page_preview=True,
        )

        finish_text = random.choice(FINISH_VARIANTS)

        await message.answer(
            f"🎉 <b>Заказ #{order_num} принят!</b>\n\n"
            f"🥤 {drink} × {quantity}\n"
            f"💰 {total}₽\n\n"
            f"{finish_text}",
            reply_markup=create_menu_keyboard(),
        )
        await state.clear()
        return

    if message.text == "Меню":
        await state.clear()
        await message.answer("☕ Меню:", reply_markup=create_menu_keyboard())
        return

    await message.answer("❌ Нажмите кнопку", reply_markup=create_confirm_keyboard())


@router.message(F.text == "📞 Позвонить")
async def call_phone(message: Message):
    name = get_user_name(message)
    if is_cafe_open():
        text = (
            f"{name}, буду рад помочь!\n\n"
            f"📞 <b>Телефон {CAFE_NAME}:</b>\n<code>{CAFE_PHONE}</code>\n\n"
            f"Если удобнее — можешь просто выбрать напиток в меню, я всё оформлю здесь."
        )
        await message.answer(text, reply_markup=create_menu_keyboard())
    else:
        text = (
            f"{name}, сейчас мы закрыты, но я всё равно подскажу.\n\n"
            f"📞 <b>Телефон {CAFE_NAME}:</b>\n<code>{CAFE_PHONE}</code>\n\n"
            f"⏰ {get_work_status()}\n\n"
            f"Хочешь — посмотри меню, а заказ оформим, как только откроемся."
        )
        await message.answer(text, reply_markup=create_info_keyboard())


@router.message(F.text == "⏰ Часы работы")
async def show_hours(message: Message):
    name = get_user_name(message)
    msk_time = get_moscow_time().strftime("%H:%M")
    if is_cafe_open():
        text = (
            f"{name}, мы сейчас на месте и готовим вкусное.\n\n"
            f"🕐 <b>Сейчас:</b> {msk_time} (МСК)\n"
            f"🏪 {get_work_status()}\n\n"
            f"📞 Если нужно уточнить детали: <code>{CAFE_PHONE}</code>\n"
            f"Выбирай напиток в меню — оформлю заказ за минуту."
        )
        await message.answer(text, reply_markup=create_menu_keyboard())
    else:
        text = (
            f"{name}, спасибо что заглянул!\n\n"
            f"🕐 <b>Сейчас:</b> {msk_time} (МСК)\n"
            f"🏪 {get_work_status()}\n\n"
            f"📞 Телефон: <code>{CAFE_PHONE}</code>\n"
            f"Пока можем показать меню — напиши /start."
        )
        await message.answer(text, reply_markup=create_info_keyboard())


@router.message(Command("stats"))
async def stats_command(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        r_client = await get_redis_client()
        total_orders = int(await r_client.get(f"stats:{CAFE_ID}:total_orders") or 0)
        stats_text = (
            f"📊 <b>Статистика заказов</b>\n"
            f"Кафе: <b>{CAFE_NAME}</b> (id={CAFE_ID})\n\n"
            f"Всего заказов: <b>{total_orders}</b>\n\n"
        )
        for drink in MENU.keys():
            count = int(await r_client.get(f"stats:{CAFE_ID}:drink:{drink}") or 0)
            if count > 0:
                stats_text += f"{drink}: {count}\n"
        await r_client.aclose()
        await message.answer(stats_text)
    except Exception:
        await message.answer("❌ Ошибка статистики")


# -------------------------
# /help — оставляем, но не публикуем в меню
# -------------------------

@router.message(Command("help"))
async def help_command(message: Message):
    text = (
        "Этот бот — демо-ассистент для кофейни.\n\n"
        "Что он умеет:\n"
        "• Показывать меню и часы работы\n"
        "• Принимать быстрые заказы прямо в чате\n"
        "• Отправлять уведомления администратору с данными клиента\n"
        "• Позволять администратору сразу открыть чат с гостем\n\n"
        "Наша фишка — тёплые ответы:\n"
        "• Разные варианты приветствия в начале диалога\n"
        "• Живые комментарии к выбору напитка\n"
        "• Дружелюбные завершения заказа с приглашением вернуться\n"
        "Бот говорит с гостем так, будто это внимательный бариста.\n\n"
        "Для владельцев кафе:\n"
        "• Это демо сервиса CafeBotify — «бот вместо администратора»\n"
        "• Подключение от 2 990 ₽ в месяц за одну точку\n\n"
        "Хотите такой бот для своей кофейни?\n"
        "Лендинг: https://your-landing.com\n"
        "Связаться в Telegram: @ВАШ_ЮЗЕРНЕЙМ"
    )
    await message.answer(text)


# -------------------------
# Startup / Webhook
# -------------------------

async def set_bot_commands(bot: Bot) -> None:
    """
    Устанавливаем только /start и /stats (для админа), без /help.
    Команды появляются в меню Telegram через setMyCommands.[web:31][web:35]
    """
    commands = [
        BotCommand(command="start", description="Запуск бота"),
        BotCommand(command="stats", description="Статистика (для админа)"),
    ]
    await bot.set_my_commands(commands)


async def on_startup(bot: Bot) -> None:
    logger.info("🚀 Запуск бота (START v1.0 DEMO)...")
    logger.info(f"☕ Кафе: {CAFE_NAME} (id={CAFE_ID})")
    logger.info(f"⏰ Часы работы: {WORK_START}:00–{WORK_END}:00 (МСК)")
    logger.info(f"⏳ Rate-limit: {RATE_LIMIT_SECONDS} сек. (только после подтверждения)")
    logger.info(f"🔗 Webhook (target): {WEBHOOK_URL}")

    try:
        r_test = redis.from_url(REDIS_URL)
        await r_test.ping()
        await r_test.aclose()
        logger.info("✅ Redis подключён")
    except Exception as e:
        logger.error(f"❌ Redis: {e}")

    try:
        current_webhook = await bot.get_webhook_info()
        logger.info(f"Текущий webhook: {current_webhook.url}")

        await bot.set_webhook(
            WEBHOOK_URL,
            secret_token=WEBHOOK_SECRET,
        )
        logger.info("✅ Webhook (re)set выполнен")
    except Exception as e:
        logger.error(f"❌ Webhook ошибка: {e}")

    # ВАЖНО: установить команды (без /help)
    try:
        await set_bot_commands(bot)
        logger.info("✅ Команды бота установлены (без /help)")
    except Exception as e:
        logger.error(f"❌ Ошибка установки команд: {e}")


async def main():
    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN не найден!")
        return
    if not REDIS_URL:
        logger.error("❌ REDIS_URL не найден!")
        return

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    storage = RedisStorage.from_url(REDIS_URL)
    dp = Dispatcher(storage=storage)
    dp.include_router(router)

    dp.startup.register(on_startup)

    app = web.Application()

    async def healthcheck(request: web.Request):
        return web.json_response({"status": "healthy", "bot": "ready"})

    app.router.add_get("/", healthcheck)

    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET,
        handle_in_background=True,
    ).register(app, path=WEBHOOK_PATH)

    setup_application(app, dp, bot=bot)

    async def _on_shutdown(a: web.Application):
        try:
            await bot.delete_webhook()
        except Exception:
            pass
        try:
            await storage.close()
        except Exception:
            pass
        try:
            await bot.session.close()
        except Exception:
            pass
        logger.info("🛑 Бот остановлен")

    app.on_shutdown.append(_on_shutdown)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)

    logger.info(f"🌐 Сервер запущен на 0.0.0.0:{PORT}")
    await site.start()

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
