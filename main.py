import logging
import os
import re
import random
import json
from datetime import datetime, timedelta
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.contrib.fsm_storage.memory import MemoryStorage

# ================== КОНФИГУРАЦИЯ ==================
def load_config():
    """Загружает настройки кафе из config.json"""
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            config = json.load(f)["cafe"]
            config["admin_chat_id"] = int(config["admin_chat_id"])
            return config
    except Exception as e:
        logging.error(f"config.json ошибка: {e}")
        return {}

CAFE = load_config()

# ================== ТЕКСТЫ ==================
ORDER_COMPLIMENTS = [
    "Отличный выбор 😊", "Хороший вкус ☕", "Популярный напиток ❤️", 
    "Ваш любимый вариант ✨"
]

ORDER_THANKS = [
    "Спасибо! Уже готовим ☕", "Заказ принят 😊", "Ждём вас! ✨"
]

BOOKING_THANKS = [
    "Заявка принята! 📞", "Скоро перезвоним 😊", "Бронь подтверждена ✅"
]

# ================== ИНИЦИАЛИЗАЦИЯ ==================
logging.basicConfig(level=logging.INFO)
load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN or ' ' in TOKEN:
    logging.error("TELEGRAM_TOKEN не найден!")
    exit(1)

bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

def get_main_menu():
    """ЧИСТОЕ меню для клиента"""
    menu = ReplyKeyboardMarkup(resize_keyboard=True)
    for item, price in CAFE.get("menu", {}).items():
        menu.add(KeyboardButton(f"{item} — {price}₽"))
    menu.add(KeyboardButton("📋 Бронь столика"))
    menu.add(KeyboardButton("❓ Помощь"))
    return menu

MAIN_MENU = get_main_menu()

# ================== FSM СОСТОЯНИЯ ==================
class OrderForm(StatesGroup):
    waiting_quantity = State()
    waiting_confirm = State()

class BookingForm(StatesGroup):
    waiting_datetime = State()
    waiting_people = State()

# ================== /START ==================
@dp.message_handler(commands=["start"])
async def start(message: types.Message):
    await message.reply(
        f"👋 Добро пожаловать в **{CAFE.get('name', 'Кофейню')}** ☕\n\n"
        f"Выберите товар из меню ниже:",
        reply_markup=MAIN_MENU,
        parse_mode="Markdown"
    )

# ================== ЗАКАЗЫ ☕ ==================
@dp.message_handler(lambda m: any(f"{item} — {price}₽" == m.text.strip() for item, price in CAFE.get("menu", {}).items()))
async def start_order(message: types.Message, state: FSMContext):
    for item_name, price in CAFE.get("menu", {}).items():
        if f"{item_name} — {price}₽" == message.text.strip():
            await state.finish()
            await state.update_data(item=item_name, price=price)
            
            kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
            kb.row("1", "2", "3+")
            kb.row("❌ Отмена")
            
            await message.reply(
                f"**{item_name}** — {price}₽\n\n"
                f"{random.choice(ORDER_COMPLIMENTS)}\n\n"
                "**Сколько порций?**",
                reply_markup=kb,
                parse_mode="Markdown"
            )
            await OrderForm.waiting_quantity.set()
            return

@dp.message_handler(state=OrderForm.waiting_quantity)
async def process_quantity(message: types.Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.finish()
        await message.reply("❌ Заказ отменён", reply_markup=MAIN_MENU)
        return

    qty_map = {"1": 1, "2": 2, "3+": 3}
    if message.text not in qty_map:
        await message.reply("❌ Выберите: **1**, **2**, **3+** или **❌ Отмена**", parse_mode="Markdown")
        return

    qty = qty_map[message.text]
    data = await state.get_data()
    total = data["price"] * qty
    await state.update_data(quantity=qty, total=total)

    kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.row("✅ Подтвердить", "❌ Отмена")

    await message.reply(
        f"**📋 Ваш заказ:**\n\n"
        f"`{data['item']}` × **{qty}**\n"
        f"**Итого:** `{total}₽`\n\n"
        "**Подтвердить?**",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    await OrderForm.waiting_confirm.set()

@dp.message_handler(state=OrderForm.waiting_confirm)
async def confirm_order(message: types.Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.finish()
        await message.reply("❌ Заказ отменён", reply_markup=MAIN_MENU)
        return

    data = await state.get_data()
    admin_id = CAFE.get("admin_chat_id")
    
    if not admin_id:
        await message.reply("❌ Ошибка конфигурации!")
        await state.finish()
        return

    try:
        await bot.send_message(
            admin_id,
            f"☕ **НОВЫЙ ЗАКАЗ** `{CAFE.get('name')}`\n\n"
            f"**{data['item']}** × {data['quantity']}\n"
            f"💰 **{data['total']}₽**\n\n"
            f"👤 @{message.from_user.username or str(message.from_user.id)}\n"
            f"🆔 `{message.from_user.id}`\n"
            f"📞 {CAFE.get('phone', '+7 (XXX) XXX-XX-XX')}",
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"Ошибка уведомления админа: {e}")

    await message.reply(
        f"🎉 **Заказ принят!**\n\n"
        f"{random.choice(ORDER_THANKS)}\n\n"
        f"📞 **{CAFE.get('phone', '+7 (XXX) XXX-XX-XX')}**",
        reply_markup=MAIN_MENU,
        parse_mode="Markdown"
    )
    await state.finish()

# ================== БРОНЬ СТОЛИКА ==================
@dp.message_handler(lambda m: m.text == "📋 Бронь столика")
async def book_start(message: types.Message, state: FSMContext):
    await state.finish()
    work_hours = CAFE.get("work_hours", [9, 22])
    start_h, end_h = work_hours
    
    await message.reply(
        f"**📅 БРОНЬ СТОЛИКА** `{CAFE.get('name')}`\n\n"
        f"`ДД.ММ ЧЧ:ММ`\n"
        f"**Пример:** `15.02 19:00`\n\n"
        f"🕐 Работаем: **{start_h}:00–{end_h}:00**",
        parse_mode="Markdown"
    )
    await BookingForm.waiting_datetime.set()

@dp.message_handler(state=BookingForm.waiting_datetime)
async def parse_datetime(message: types.Message, state: FSMContext):
    match = re.match(r"^(\d{1,2})\.(\d{1,2})\s+(\d{2}):(\d{2})$", message.text.strip())
    if not match:
        await message.reply("❌ **Неверный формат!**\n\n`15.02 19:00`", parse_mode="Markdown")
        return

    day, month, hour, minute = map(int, match.groups())
    now = datetime.now()
    work_hours = CAFE.get("work_hours", [9, 22])
    start_h, end_h = work_hours

    try:
        booking_dt = now.replace(day=day, month=month, hour=hour, minute=minute)
        if booking_dt <= now:
            booking_dt += timedelta(days=1)

        if hour < start_h or hour >= end_h:
            await message.reply(f"❌ Мы работаем **{start_h}:00–{end_h}:00**", parse_mode="Markdown")
            return

        await state.update_data(dt=booking_dt)

        kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        kb.row("1-2", "3-4")
        kb.row("5+", "❌ Отмена")

        await message.reply(
            f"✅ **{booking_dt.strftime('%d.%m %H:%M')}**\n\n**👥 Сколько человек?**",
            reply_markup=kb,
            parse_mode="Markdown"
        )
        await BookingForm.waiting_people.set()

    except Exception:
        await message.reply("❌ Формат: `15.02 19:00`", parse_mode="Markdown")

@dp.message_handler(state=BookingForm.waiting_people)
async def finish_booking(message: types.Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.finish()
        await message.reply("❌ Заявка отменена", reply_markup=MAIN_MENU)
        return

    people_map = {"1-2": 2, "3-4": 4, "5+": 6}
    if message.text not in people_map:
        await message.reply("❌ Выберите: **1-2**, **3-4**, **5+**", parse_mode="Markdown")
        return

    people = people_map[message.text]
    data = await state.get_data()
    admin_id = CAFE.get("admin_chat_id")

    if not admin_id:
        await message.reply("❌ Ошибка конфигурации!")
        await state.finish()
        return

    try:
        await bot.send_message(
            admin_id,
            f"📋 **НОВАЯ БРОНЬ** `{CAFE.get('name')}`\n\n"
            f"🕐 **{data['dt'].strftime('%d.%m %H:%M')}**\n"
            f"👥 **{people} человек**\n"
            f"👤 @{message.from_user.username or str(message.from_user.id)}\n"
            f"🆔 `{message.from_user.id}`\n"
            f"📞 {CAFE.get('phone', '+7 (XXX) XXX-XX-XX')}",
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"Ошибка брони админу: {e}")

    await message.reply(
        f"✅ **Бронь принята!**\n\n"
        f"🕐 **{data['dt'].strftime('%d.%m %H:%M')}**\n"
        f"👥 **{people} человек**\n\n"
        f"{random.choice(BOOKING_THANKS)}\n"
        f"📞 **{CAFE.get('phone', '+7 (XXX) XXX-XX-XX')}**",
        reply_markup=MAIN_MENU,
        parse_mode="Markdown"
    )
    await state.finish()

# ================== ПОМОЩЬ ==================
@dp.message_handler(lambda m: m.text == "❓ Помощь")
async def help_handler(message: types.Message):
    work_hours = CAFE.get("work_hours", [9, 22])
    start_h, end_h = work_hours
    await message.reply(
        f"**{CAFE.get('name')}** — справка ☕\n\n"
        f"☕ **Меню** — выберите товар → количество → подтвердите\n"
        f"📋 **Бронь** — дата/время → количество человек\n\n"
        f"📞 **{CAFE.get('phone', '+7 (XXX) XXX-XX-XX')}**\n"
        f"🕐 **{start_h}:00–{end_h}:00**",
        reply_markup=MAIN_MENU,
        parse_mode="Markdown"
    )

# ================== FALLBACK ==================
@dp.message_handler()
async def fallback(message: types.Message, state: FSMContext):
    await state.finish()
    await message.reply(
        f"👋 **{CAFE.get('name')}**\n\n"
        "Выберите из меню ☕",
        reply_markup=MAIN_MENU,
        parse_mode="Markdown"
    )

# ================== ОШИБКИ ==================
@dp.errors_handler()
async def errors_handler(update, exception):
    logging.error(f"Глобальная ошибка: {exception}")
    return True

# ================== WEBHOOK (Render) ==================
WEBHOOK_PATH = f"/webhook/{TOKEN}"
WEBHOOK_URL = f"https://chatbotify-2tjd.onrender.com{WEBHOOK_PATH}"

async def on_startup(dp):
    await bot.set_webhook(WEBHOOK_URL)
    logging.info(f"{CAFE.get('name')} запущен!")

if __name__ == "__main__":
    executor.start_webhook(
        dp, WEBHOOK_PATH, on_startup=on_startup,
        host="0.0.0.0", port=int(os.getenv("PORT", 10000))
    )
