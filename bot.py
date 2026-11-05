import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlencode

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
USERS_FILE = Path(os.getenv("USERS_DB_PATH", "users.json"))

# --- НАСТРОЙКИ ЮMONEY ---
YOOMONEY_WALLET = need("YOOMONEY_WALLET") # Твой номер кошелька (410011...)
STAR_PRICE_RUB = float(os.getenv("STAR_PRICE_RUB", "1.5")) # Сколько стоит 1 звезда в рублях

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stars-bot")

# --- ОТКЛЮЧАЕМ СПАМ ОШИБОК СЕТИ ОТ AIOGRAM ---
logging.getLogger('aiogram').setLevel(logging.CRITICAL)
logging.getLogger('aiogram.dispatcher').setLevel(logging.CRITICAL)

# --- ИНИЦИАЛИЗАЦИЯ БОТОВ ---
bot = Bot(token=BOT_TOKEN, request_timeout=30)
dp = Dispatcher()
router = Router()

shop_bot = Bot(token=SHOP_BOT_TOKEN, request_timeout=30)
shop_dp = Dispatcher(storage=MemoryStorage())
shop_router = Router()

lock = asyncio.Lock()
funpay: Account = None

# --- БАЗА ДАННЫХ ЗАКАЗОВ ---
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

# --- БАЗА ДАННЫХ ПОЛЬЗОВАТЕЛЕЙ (ДЛЯ РЕФЕРАЛОВ) ---
try:
    users: dict = json.loads(USERS_FILE.read_text("utf-8")) if USERS_FILE.exists() else {}
except Exception:
    log.exception("users.json повреждён, начинаю с пустого")
    users = {}

def save_users():
    tmp = USERS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(users, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(USERS_FILE)

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
    waiting_custom_amount = State()

def get_main_menu():
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Купить звезды", callback_data="menu_buy")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="menu_profile"), 
         InlineKeyboardButton(text="🤝 Реферальная система", callback_data="menu_ref")],
        [InlineKeyboardButton(text="🆘 Поддержка", url="https://t.me/gyjov")]
    ])
    return kb

def get_buy_menu():
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="50 ⭐", callback_data="buy_50"), 
         InlineKeyboardButton(text="100 ⭐", callback_data="buy_100")],
        [InlineKeyboardButton(text="200 ⭐", callback_data="buy_200"), 
         InlineKeyboardButton(text="500 ⭐", callback_data="buy_500")],
        [InlineKeyboardButton(text="1000 ⭐", callback_data="buy_1000")],
        [InlineKeyboardButton(text="✍️ Указать свое количество", callback_data="buy_custom")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_back")]
    ])
    return kb

@shop_router.message(CommandStart())
async def shop_start(m: Message, state: FSMContext):
    await state.clear()
    
    args = m.text.split()
    if len(args) > 1 and args[1].startswith("ref_"):
        referrer_id = args[1].replace("ref_", "")
        user_id = str(m.from_user.id)
        
        if user_id not in users and referrer_id != user_id:
            if referrer_id in users:
                users[user_id] = {"ref_by": referrer_id, "ref_count": 0, "earned": 0}
                users[referrer_id]["ref_count"] = users[referrer_id].get("ref_count", 0) + 1
                save_users()
                try:
                    await shop_bot.send_message(int(referrer_id), "🎉 По вашей ссылке зарегистрировался новый пользователь!")
                except:
                    pass
        elif user_id in users and not users[user_id].get("ref_by") and referrer_id != user_id:
             if referrer_id in users:
                users[user_id]["ref_by"] = referrer_id
                users[referrer_id]["ref_count"] = users[referrer_id].get("ref_count", 0) + 1
                save_users()

    user_id = str(m.from_user.id)
    if user_id not in users:
        users[user_id] = {"ref_by": None, "ref_count": 0, "earned": 0}
        save_users()

    await m.answer("👋 Добро пожаловать в магазин Stars!\n\nВыберите нужное действие в меню ниже:", reply_markup=get_main_menu())

@shop_router.callback_query(F.data == "menu_back")
async def menu_back(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("👋 Главное меню:", reply_markup=get_main_menu())
    await c.answer()

@shop_router.callback_query(F.data == "menu_buy")
async def menu_buy(c: CallbackQuery):
    await c.message.edit_text(
        "⭐ Выберите количество звезд для покупки.\n"
        "Минимальный заказ: 50 звезд.", 
        reply_markup=get_buy_menu()
    )
    await c.answer()

@shop_router.callback_query(F.data == "menu_profile")
async def menu_profile(c: CallbackQuery):
    user_id = str(c.from_user.id)
    u = users.get(user_id, {"ref_count": 0, "earned": 0})
    
    text = (
        f"👤 <b>Ваш профиль</b>\n\n"
        f"🆔 Ваш ID: <code>{user_id}</code>\n"
        f"👥 Приглашено людей: <b>{u.get('ref_count', 0)}</b>\n"
        f"💰 Заработано с рефералов: <b>{u.get('earned', 0)}</b> (в Stars)\n"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="menu_back")]])
    await c.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await c.answer()

@shop_router.callback_query(F.data == "menu_ref")
async def menu_ref(c: CallbackQuery):
    user_id = str(c.from_user.id)
    bot_info = await shop_bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{user_id}"
    
    text = (
        f"🤝 <b>Реферальная система</b>\n\n"
        f"Приглашайте друзей и получайте 10% от их покупок!\n\n"
        f"🔗 Ваша ссылка для приглашения:\n"
        f"<code>{ref_link}</code>\n\n"
        f"Отправьте её друзьям. Когда они купят звезды, вы получите вознаграждение в Stars на свой баланс (выплаты производятся вручную)."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="menu_back")]])
    await c.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await c.answer()

# --- ПОКУПКА ЗВЕЗД ---
@shop_router.callback_query(F.data.startswith("buy_") & ~F.data.contains("custom"))
async def process_buy(c: CallbackQuery, state: FSMContext):
    amount = int(c.data.split("_")[1])
    await state.update_data(amount=amount)
    await state.set_state(ShopStates.waiting_username)
    
    await c.message.edit_text(
        f"Вы выбрали <b>{amount}</b> Stars.\n\n"
        f"Пожалуйста, отправьте Telegram username (например, @username), "
        f"на который нужно зачислить Stars.",
        parse_mode="HTML"
    )
    await c.answer()

@shop_router.callback_query(F.data == "buy_custom")
async def process_custom_buy(c: CallbackQuery, state: FSMContext):
    await state.set_state(ShopStates.waiting_custom_amount)
    await c.message.edit_text(
        "✍️ Введите желаемое количество звезд.\n"
        f"Минимальное количество: <b>{MIN_STARS}</b> звезд.",
        parse_mode="HTML"
    )
    await c.answer()

@shop_router.message(ShopStates.waiting_custom_amount)
async def process_custom_amount(m: Message, state: FSMContext):
    if not m.text.isdigit():
        return await m.answer("❌ Пожалуйста, введите целое число.")
    
    amount = int(m.text)
    if amount < MIN_STARS:
        return await m.answer(f"❌ Минимальное количество звезд: {MIN_STARS}.")
    
    await state.update_data(amount=amount)
    await state.set_state(ShopStates.waiting_username)
    await m.answer(
        f"Вы выбрали <b>{amount}</b> Stars.\n\n"
        f"Пожалуйста, отправьте Telegram username (например, @username), "
        f"на который нужно зачислить Stars.",
        parse_mode="HTML"
    )

@shop_router.message(ShopStates.waiting_username)
async def get_username(m: Message, state: FSMContext):
    username = m.text.strip()
    if not username.startswith("@") or len(username) < 5:
        return await m.answer("❌ Пожалуйста, отправьте username в правильном формате (например, @username).")
    
    data = await state.get_data()
    amount = data.get("amount")
    user_id = str(m.from_user.id)
    oid = f"SHOP_{m.from_user.id}_{int(m.date.timestamp())}"
    
    # Считаем сумму к оплате
    try:
        price_rub = int(amount * STAR_PRICE_RUB)
    except Exception as e:
        log.error(f"Ошибка подсчета цены: {e}. Проверь STAR_PRICE_RUB в Railway!")
        price_rub = 0
    
    # Генерируем ссылку на оплату через Quickpay (без токена)
    params = {
        "receiver": YOOMONEY_WALLET,
        "quickpay-form": "shop",
        "targets": f"Оплата заказа {oid}",
        "paymentType": "SB", # СБП/карта
        "sum": price_rub,
        "label": oid,
        "successURL": "https://t.me/DopingStars_bot" # Куда вернуть после оплаты
    }
    pay_url = "https://yoomoney.ru/quickpay/confirm.xml?" + urlencode(params)
    
    async with lock:
        orders[oid] = {
            "buyer": m.from_user.username or user_id,
            "chat_id": m.chat.id,
            "amount": amount,
            "username": username.replace("@", ""),
            "status": "waiting_payment", # Ждем оплаты
            "source": "shop",
            "tries": 0
        }
        save()
    
    # Начисление реферальных
    referrer_id = users.get(user_id, {}).get("ref_by")
    if referrer_id and referrer_id in users:
        earned = int(amount * 0.10)
        users[referrer_id]["earned"] = users[referrer_id].get("earned", 0) + earned
        save_users()
        try:
            await shop_bot.send_message(int(referrer_id), f"💰 Вам начислено <b>{earned} Stars</b> за покупку вашего реферала!", parse_mode="HTML")
        except:
            pass
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Перейти к оплате", url=pay_url)],
        [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"paid_{oid}")]
    ])
    
    await m.answer(
        f"Заказ на {amount} Stars создан!\n"
        f"Сумма к оплате: <b>{price_rub} руб.</b>\n\n"
        f"1. Нажмите «Перейти к оплате».\n"
        f"2. Переведите точную сумму.\n"
        f"3. Вернитесь и нажмите «Я оплатил».",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await state.clear()

@shop_router.callback_query(F.data.startswith("paid_"))
async def process_paid(c: CallbackQuery):
    oid = c.data.split("_")[1]
    async with lock:
        o = orders.get(oid)
        if not o or o["status"] != "waiting_payment":
            return await c.answer("Заказ не найден или уже обработан.", show_alert=True)
        o["status"] = "ready" 
        save()
    
    await c.message.edit_text("⏳ Заказ отправлен на проверку администратору. Ожидайте выдачи Stars.")
    await c.answer()
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Выдать Stars", callback_data=f"ok:{oid}"),
        InlineKeyboardButton(text="❌ Отменить", callback_data=f"no:{oid}"),
    ]])
    
    # Логируем, что мы пытаемся отправить сообщение
    log.info(f"Пытаюсь отправить уведомление админу {ADMIN_ID} по заказу {oid}")
    
    try:
        try:
            price_rub = int(o['amount'] * STAR_PRICE_RUB)
        except Exception as e:
            price_rub = 0
            log.error(f"Ошибка подсчета цены: {e}.")
            
        await bot.send_message(
            ADMIN_ID,
            f"💰 <b>Покупатель нажал «Я оплатил»!</b>\n"
            f"Проверь поступление {price_rub} руб. на ЮMoney.\n\n"
            + order_text(oid, o), 
            reply_markup=kb,
            parse_mode="HTML"
        )
        log.info("Уведомление админу успешно отправлено!")
    except Exception as e:
        log.error(f"ОШИБКА ОТПРАВКИ АДМИНУ: {e}", exc_info=True)

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
    
    try:
        await bot.send_message(ADMIN_ID, f"✅ Основной бот и Магазин запущены. Автовыдача: {'ВКЛ' if AUTO_DELIVERY else 'ВЫКЛ'}")
    except Exception as e:
        log.error(f"Не удалось отправить сообщение админу. Ошибка: {e}")
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
