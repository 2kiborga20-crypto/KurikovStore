# shared_db.py
import asyncio
import json
import logging
from pathlib import Path
from pyfragment import FragmentClient
from pyfragment import UserNotFoundError

log = logging.getLogger("shared")
DB_FILE = Path("orders.json")
lock = asyncio.Lock()
orders = {}

# Загрузка базы при старте
if DB_FILE.exists():
    try:
        orders = json.loads(DB_FILE.read_text("utf-8"))
    except Exception:
        log.exception("Ошибка чтения базы, создаем новую")

def save_orders():
    tmp = DB_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(orders, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(DB_FILE)

async def create_order(oid: str, buyer: str, chat_id: int, amount: int, username: str, source: str, admin_bot):
    """Создает заказ в базе и уведомляет админа через основного бота"""
    async with lock:
        orders[oid] = {
            "buyer": buyer,
            "chat_id": chat_id,
            "amount": amount,
            "username": username,
            "status": "processing", # Сразу в обработку, так как оплата уже есть
            "source": source,
            "tries": 0
        }
        save_orders()
    
    # Уведомляем вас (админа) в основном боте
    try:
        await admin_bot.send_message(
            int(os.getenv("ADMIN_ID")), 
            f"🆕 Новый заказ из TG-Магазина!\nПокупатель: {buyer}\nStars: {amount}\nПолучатель: @{username}"
        )
    except Exception:
        log.exception("Не удалось уведомить админа")

async def buy_stars(ton_seed, tonapi_key, fragment_cookies, wallet_version, username, amount):
    """Логика покупки Stars"""
    try:
        async with FragmentClient(
            seed=ton_seed,
            api_key=tonapi_key,
            cookies=fragment_cookies,
            wallet_version=wallet_version,
        ) as client:
            result = await client.purchase_stars(f"@{username}", amount=amount)
        return True, result
    except UserNotFoundError:
        return False, "Пользователь не найден на Fragment"
    except Exception as e:
        return False, str(e)
