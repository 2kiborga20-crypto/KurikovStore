# shop_bot.py
import os
import logging
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv
from shared_db import create_order, buy_stars, orders, save_orders

load_dotenv()

SHOP_TOKEN = os.getenv("SHOP_BOT_TOKEN") # Токен НОВОГО бота
ADMIN_ID = int(os.getenv("ADMIN_ID"))

TON_SEED = os.getenv("TON_SEED")
TONAPI_KEY = os.getenv("TONAPI_KEY")
WALLET_VERSION = os.getenv("WALLET_VERSION", "V5R1")
FRAGMENT_COOKIES = {
    "stel_ssid": os.getenv("FRAGMENT_SSID"),
    "stel_dt": os.getenv("FRAGMENT_DT"),
    "stel_token": os.getenv("FRAGMENT_TOKEN"),
    "stel_ton_token": os.getenv("FRAGMENT_TON_TOKEN"),
}

shop_bot = Bot(token=SHOP_TOKEN)
shop_dp = Dispatcher(storage=MemoryStorage())

# Для уведомления админа мы будем использовать этот же бот
admin_bot_proxy = shop_bot 

class ShopStates(StatesGroup):
    waiting_username = State()

@shop_dp.message(Command("start"))
async def shop_start(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Купить 100 Stars", callback_data="buy_100")],
        [InlineKeyboardButton(text="Купить 500 Stars", callback_data="buy_500")],
        [InlineKeyboardButton(text="Купить 1000 Stars", callback_data="buy_1000")],
    ])
    await m.answer("Добро пожаловать в магазин Stars! Выберите пакет:", reply_markup=kb)

@shop_dp.callback_query(F.data.startswith("buy_"))
async def process_buy(c: CallbackQuery, state: FSMContext):
    amount = int(c.data.split("_")[1])
    
    # Здесь в реальности должна быть проверка оплаты!
    # Допустим, мы считаем, что оплата прошла.
    
    await state.update_data(amount=amount)
    await state.set_state(ShopStates.waiting_username)
    
    await c.message.edit_text(
        f"Вы выбрали {amount} Stars.\n\n"
        f"Пожалуйста, отправьте Telegram username (например, @username), "
        f"на который нужно зачислить Stars."
    )
    await c.answer()

@shop_dp.message(ShopStates.waiting_username)
async def get_username(m: Message, state: FSMContext):
    username = m.text.strip()
    if not username.startswith("@") or len(username) < 5:
        return await m.answer("Пожалуйста, отправьте username в правильном формате (например, @username).")
    
    data = await state.get_data()
    amount = data.get("amount")
    
    # Генерируем уникальный ID заказа для магазина
    oid = f"SHOP_{m.from_user.id}_{m.message_id}"
    
    await m.answer(f"Заказ на {amount} Stars для {username} создан. Выдаю...")
    
    # Создаем заказ в базе (синхронизируем с основным ботом)
    await create_order(
        oid=oid,
        buyer=m.from_user.username or str(m.from_user.id),
        chat_id=m.chat.id,
        amount=amount,
        username=username.replace("@", ""),
        source="shop",
        admin_bot=admin_bot_proxy
    )
    
    # Пытаемся купить Stars
    success, result = await buy_stars(
        TON_SEED, TONAPI_KEY, FRAGMENT_COOKIES, WALLET_VERSION, 
        username.replace("@", ""), amount
    )
    
    if success:
        await m.answer(f"✅ Stars ({amount} шт.) успешно отправлены на {username}!")
        # Обновляем статус в базе
        async with shared_db.lock:
            if oid in orders:
                orders[oid]["status"] = "done"
                save_orders()
    else:
        await m.answer(f"⚠️ Произошла ошибка при выдаче: {result}\nАдминистратор уже уведомлен, с вами свяжутся.")
        async with shared_db.lock:
            if oid in orders:
                orders[oid]["status"] = "failed"
                save_orders()
                
    await state.clear()

async def start_shop():
    logging.info("Запуск магазина...")
    await shop_dp.start_polling(shop_bot)
