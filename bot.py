import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv
from aioFunPayAPI import Account, Runner
from aioFunPayAPI.common.enums import EventTypes
import pyfragment
from pyfragment import FragmentClient

load_dotenv()

def need(name: str) -> str:
    value = os.getenv(name)
    if not value:
        sys.exit(f"Не задана переменная окружения {name}")
    return value

# --- НАСТРОЙКИ ---
BOT_TOKEN = need("BOT_TOKEN")           # Токен основного бота (админка)
SHOP_BOT_TOKEN = need("SHOP_BOT_TOKEN") # Токен нового бота (магазин)
ADMIN_ID = int(need("ADMIN_ID"))
GOLDEN_KEY = need("FUNPAY_GOLDEN_KEY")

TON_SEED = need("TON_SEED")
TONAPI_KEY = need("TONAPI_KEY")
WALLET_VERSION = os.getenv("WALLET_VERSION", "V5R1")
FRAGMENT_COOKIES = {
    "stel_ssid": need("FRAGMENT_SSID"),
    "stel_dt": need("FRAGMENT_DT"),
    "stel_token": need("FRAGMENT_TOKEN"),
    "stel_ton_token": need("FRAGMENT_TON_TOKEN"),
}
MIN_STARS = 50
AUTO_DELIVERY = os.getenv("AUTO_DELIVERY", "0") == "1"
MAX_AUTO_AMOUNT = int(os.getenv("MAX_AUTO_AMOUNT", "500"))
DB_FILE = Path(os.getenv("DB_PATH", "orders.json"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stars-bot")

# --- ИНИЦИАЛИЗАЦИЯ БОТОВ ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()

shop_bot = Bot(token=SHOP_BOT_TOKEN)
shop_dp = Dispatcher(storage=MemoryStorage())
shop_router = Router()

lock = asyncio.Lock()
funpay: Account = None

# --- БАЗА ДАННЫХ ---
DB_FILE.parent.mkdir(parents=True, exist_ok=True)
try:
    orders: dict = json.loads(DB_FILE.read_text("utf-8")) if DB_FILE.exists() else {}
except Exception:
    log.exception("orders.json повреждён, начинаю с пустого")
    orders = {}

def save():
    tmp = DB_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(orders, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(DB_FILE)

async def notify(text: str, **kw):
    await bot.send_message(ADMIN_ID, text, **kw)

def order_text(oid: str, o: dict) -> str:
    source = "🛍 TG-Магазин" if o.get("source") == "shop" else "🎮 FunPay"
    return (f"🛒 Заказ #{oid} ({source})\n"
            f"Покупатель: {o['buyer']}\n"
            f"Stars: {o['amount'] or 'не определено'}\n"
            f"Получатель: @{o.get('username') or '—'}\n"
            f"Статус: {o['status']}")

# ================== ЛОГИКА ПОКУПКИ STARS ==================
class RecipientNotFound(Exception):
    pass

UserNotFound = getattr(pyfragment, "UserNotFoundError", ())
buy_lock = asyncio.Lock()

async def buy_stars(username: str, amount: int) -> bool:
    if amount < MIN_STARS:
        raise ValueError(f"Fragment продаёт Stars от {MIN_STARS} штук")
    async with buy_lock:
        try:
            async with FragmentClient(
                seed=TON_SEED,
                api_key=TONAPI_KEY,
                cookies=FRAGMENT_COOKIES,
                wallet_version=WALLET_VERSION,
            ) as client:
                result = await client.purchase_stars(f"@{username}", amount=amount)
        except UserNotFound as e:
            raise RecipientNotFound(str(e)) from e
    log.info("Stars отправлены: %s", result)
    return True

async def deliver(oid: str) -> bool:
    async with lock:
        o = orders.get(oid)
        if not o or o["status"] not in ("ready", "processing"):
            return False
        o["status"] = "processing"
        save()

    err = ""
    try:
        ok = await buy_stars(o["username"], o["amount"])
    except RecipientNotFound:
        bad = o["username"]
        async with lock:
            o["username"] = None
            o["status"] = "waiting_username"
            save()
        
        # Если это заказ из магазина, пишем в магазин, иначе в FunPay
        if o.get("source") == "shop":
            await shop_bot.send_message(o["chat_id"], f"Не удалось найти @{bad} на Fragment. Проверьте username и пришлите его ещё раз.")
        else:
            await funpay.send_message(o["chat_id"], f"Не удалось найти @{bad} на Fragment. Проверьте username и пришлите его ещё раз.")
            
        await bot.send_message(ADMIN_ID, f"↩️ @{bad} не найден, попросил покупателя прислать заново.\n\n" + order_text(oid, o))
        return False
    except Exception as e:
        log.exception("Ошибка покупки Stars")
        ok, err = False, str(e)

    if not ok:
        async with lock:
            o["status"] = "failed"
            save()
        await bot.send_message(ADMIN_ID, f"⚠️ Ошибка выдачи. Проверь кошелёк вручную.\n{err}\n\n{order_text(oid, o)}")
        return False

    async with lock:
        o["status"] = "done"
        save()
        
    if o.get("source") == "shop":
        await shop_bot.send_message(o["chat_id"], f"✅ Stars ({o['amount']} шт.) успешно отправлены на @{o['username']}!")
    else:
        try:
            await funpay.send_message(o["chat_id"], f"Stars отправлены на @{o['username']}. Пожалуйста, подтвердите выполнение заказа. Спасибо!")
        except Exception:
            log.exception("Не удалось написать покупателю")
            await bot.send_message(ADMIN_ID, "⚠️ Stars выданы, но сообщение покупателю не ушло. Напиши ему сам.")
            
    await bot.send_message(ADMIN_ID, "✅ Выдано\n\n" + order_text(oid, o))
    return True

async def on_ready(oid: str):
    o = orders[oid]
    if AUTO_DELIVERY and MIN_STARS <= o["amount"] <= MAX_AUTO_AMOUNT:
        await bot.send_message(ADMIN_ID, "🤖 Автовыдача\n\n" + order_text(oid, o))
        await deliver(oid)
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Выдать Stars", callback_data=f"ok:{oid}"),
            InlineKeyboardButton(text="❌ Отменить", callback_data=f"no:{oid}"),
        ]])
        await bot.send_message(ADMIN_ID, order_text(oid, o), reply_markup=kb)

# ================== ОСНОВНОЙ БОТ (АДМИНКА) ==================
def admin_only(user_id: int) -> bool:
    return user_id == ADMIN_ID

@router.message(CommandStart())
async def cmd_start(m: Message):
    if admin_only(m.from_user.id):
        await m.answer(
            f"Бот запущен.\nАвтовыдача: {'ВКЛ' if AUTO_DELIVERY else 'ВЫКЛ'}\n"
            f"Лимит автовыдачи: {MAX_AUTO_AMOUNT} Stars\n\n"
            f"/orders — последние заказы")

@router.message(Command("orders"))
async def cmd_orders(m: Message):
    if not admin_only(m.from_user.id):
        return
    if not orders:
        return await m.answer("Заказов пока нет")
    last = list(orders.items())[-10:]
    await m.answer("\n\n".join(order_text(k, v) for k, v in last))

@router.callback_query(F.data.startswith("ok:"))
async def cb_ok(c: CallbackQuery):
    if not admin_only(c.from_user.id):
        return await c.answer("Нет доступа", show_alert=True)
    oid = c.data.split(":", 1)[1]
    if orders.get(oid, {}).get("status") != "ready":
        return await c.answer("Заказ уже обработан или не готов", show_alert=True)
    await c.message.edit_reply_markup(reply_markup=None)
    await c.answer("Выдаю...")
    await deliver(oid)

@router.callback_query(F.data.startswith("no:"))
async def cb_no(c: CallbackQuery):
    if not admin_only(c.from_user.id):
        return await c.answer("Нет доступа", show_alert=True)
    oid = c.data.split(":", 1)[1]
    async with lock:
        o = orders.get(oid)
        if not o or o["status"] not in ("ready", "waiting_username", "waiting_confirm", "failed"):
            return await c.answer("Заказ уже обработан", show_alert=True)
        o["status"] = "cancelled"
        save()
    result = "❌ Отменён"
    if o.get("source") != "shop":
        try:
            await funpay.refund(oid)
            result += ", деньги возвращены покупателю на FunPay"
        except Exception as e:
            log.exception("Не удалось сделать возврат на FunPay")
            result += f"\n⚠️ Возврат не удался ({e}). Сделай его на FunPay вручную."
    await c.message.edit_text(c.message.text + "\n\n" + result, reply_markup=None)
    await c.answer()

# ================== МАГАЗИН (ВТОРОЙ БОТ) ==================
class ShopStates(StatesGroup):
    waiting_username = State()

@shop_router.message(CommandStart())
async def shop_start(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Купить 100 Stars", callback_data="buy_100")],
        [InlineKeyboardButton(text="Купить 500 Stars", callback_data="buy_500")],
        [InlineKeyboardButton(text="Купить 1000 Stars", callback_data="buy_1000")],
    ])
    await m.answer("Добро пожаловать в магазин Stars! Выберите пакет:", reply_markup=kb)

@shop_router.callback_query(F.data.startswith("buy_"))
async def process_buy(c: CallbackQuery, state: FSMContext):
    amount = int(c.data.split("_")[1])
    
    # ВНИМАНИЕ: Здесь должна быть интеграция с платежной системой!
    # Сейчас бот считает, что оплата уже прошла.
    
    await state.update_data(amount=amount)
    await state.set_state(ShopStates.waiting_username)
    
    await c.message.edit_text(
        f"Вы выбрали {amount} Stars.\n\n"
        f"Пожалуйста, отправьте Telegram username (например, @username), "
        f"на который нужно зачислить Stars."
    )
    await c.answer()

@shop_router.message(ShopStates.waiting_username)
async def get_username(m: Message, state: FSMContext):
    username = m.text.strip()
    if not username.startswith("@") or len(username) < 5:
        return await m.answer("Пожалуйста, отправьте username в правильном формате (например, @username).")
    
    data = await state.get_data()
    amount = data.get("amount")
    oid = f"SHOP_{m.from_user.id}_{int(m.date.timestamp())}"
    
    async with lock:
        orders[oid] = {
            "buyer": m.from_user.username or str(m.from_user.id),
            "chat_id": m.chat.id,
            "amount": amount,
            "username": username.replace("@", ""),
            "status": "processing",
            "source": "shop",
            "tries": 0
        }
        save()
    
    await m.answer(f"Заказ на {amount} Stars для {username} создан. Выдаю...")
    await notify(f"🆕 Новый заказ из TG-Магазина!\n\n" + order_text(oid, orders[oid]))
    
    await deliver(oid)
    await state.clear()

# ================== FUNPAY СЛУШАТЕЛЬ ==================
USERNAME_FULL = re.compile(r"@?([A-Za-z][A-Za-z0-9_]{4,31})")
USERNAME_AT = re.compile(r"(?:@|t\.me/)([A-Za-z][A-Za-z0-9_]{4,31})")
YES = {"да", "+", "yes", "y", "верно", "ок", "ok", "ага", "подтверждаю"}

def extract_username(text: str):
    text = (text or "").strip()
    m = USERNAME_AT.search(text) or USERNAME_FULL.fullmatch(text)
    return m.group(1) if m else None

def parse_amount(description: str, qty) -> int:
    m = re.search(r"\d+", description or "")
    if not m:
        return 0
    return int(m.group()) * (int(qty) if qty else 1)

async def handle_new_order(event):
    short = event.order
    oid = str(short.id).lstrip("#")
    async with lock:
        if oid in orders:
            return
    
    full = await funpay.get_order(oid)
    amount = parse_amount(short.description, getattr(short, "amount", 1))
    
    async with lock:
        orders[oid] = {
            "buyer": short.buyer_username,
            "chat_id": full.chat_id,
            "amount": amount,
            "username": None,
            "status": "waiting_username",
            "source": "funpay",
            "tries": 0,
        }
        save()
        
    await funpay.send_message(full.chat_id, "Здравствуйте! Напишите, пожалуйста, ваш Telegram username (например @username), на который отправить Stars.")
    await notify("🆕 Новый заказ с FunPay, жду username от покупателя\n\n" + order_text(oid, orders[oid]))

async def handle_new_message(event):
    msg = event.message
    if getattr(msg, "by_bot", False) or msg.author_id == funpay.id:
        return
    text = (msg.text or "").strip()

    async with lock:
        target = next(((oid, o) for oid, o in orders.items()
                       if o.get("source") == "funpay" 
                       and o["chat_id"] == msg.chat_id
                       and o["status"] in ("waiting_username", "waiting_confirm")), None)
        if not target:
            return
        oid, o = target
        username = extract_username(text)

        if o["status"] == "waiting_confirm" and text.lower() in YES:
            o["status"] = "ready"
            save()
            reply = f"Принято, отправляю Stars на @{o['username']}."
            ready = True
        elif username:
            o["username"] = username
            o["status"] = "waiting_confirm"
            save()
            reply = f"Отправить Stars на @{username}? Если всё верно, ответьте «да»."
            ready = False
        else:
            o["tries"] = o.get("tries", 0) + 1
            save()
            if o["tries"] > 3: return
            reply = "Не удалось распознать username. Пришлите его в формате @username."
            ready = False

    await funpay.send_message(msg.chat_id, reply)
    if ready:
        await on_ready(oid)

async def connect():
    global funpay
    acc = Account(GOLDEN_KEY)
    funpay = await acc.get()

async def funpay_listener():
    delay = 5
    first = True
    while True:
        try:
            if not first: await connect()
            first = False
            runner = Runner(funpay)
            delay = 5
            async for event in runner.listen(requests_delay=4):
                try:
                    if event.type == EventTypes.NEW_ORDER:
                        await handle_new_order(event)
                    elif event.type == EventTypes.NEW_MESSAGE:
                        await handle_new_message(event)
                except Exception:
                    log.exception("Ошибка обработки события FunPay")
        except Exception as e:
            log.exception("Сбой соединения с FunPay")
            await notify(f"⚠️ FunPay: сбой ({e}). Переподключаюсь через {delay} с.")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)

# ================== ЗАПУСК ==================
async def recover():
    for oid, o in list(orders.items()):
        if o["status"] == "processing":
            o["status"] = "failed"
            save()
            await bot.send_message(ADMIN_ID, "⚠️ Бот перезапустился во время выдачи. Проверь кошелёк.\n\n" + order_text(oid, o))
        elif o["status"] == "ready":
            await on_ready(oid)

async def main():
    try:
        await connect()
    except Exception as e:
        sys.exit(f"Не удалось войти на FunPay: {e}")
    
    log.info(f"FunPay: вошёл как {funpay.username}")

    dp.include_router(router)
    shop_dp.include_router(shop_router)
    
    await recover()
    
    asyncio.create_task(funpay_listener())
    asyncio.create_task(shop_dp.start_polling(shop_bot))
    
    await bot.send_message(ADMIN_ID, f"✅ Основной бот и Магазин запущены. Автовыдача: {'ВКЛ' if AUTO_DELIVERY else 'ВЫКЛ'}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
