import os, asyncio, logging, hashlib, hmac, json
import psycopg2
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import datetime, timezone
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from db import init_db, add_exchange, dashboard, recent_exchanges, add_expense, partner_report, add_payout, audit
from webapp import create_app

logging.basicConfig(level=logging.INFO)
TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
DENIS_ID = int(os.getenv("DENIS_ID", "0"))
EGOR_ID = int(os.getenv("EGOR_ID", "0"))
API_SECRET = os.getenv("API_SECRET", "")
PORT = int(os.getenv("PORT", "10000"))
APP_URL = os.getenv("APP_URL", "").rstrip("/")

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

class ExchangeForm(StatesGroup):
    direction = State(); received = State(); paid = State(); cost = State(); note = State()

class ExpenseForm(StatesGroup):
    currency = State(); amount = State(); description = State()

def allowed(uid): return uid in ADMIN_IDS and uid != 0
def menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="stats"),
         InlineKeyboardButton(text="➕ Новий обмін", callback_data="exchange")],
        [InlineKeyboardButton(text="📋 Історія", callback_data="history"),
         InlineKeyboardButton(text="💸 Витрата", callback_data="expense")],
        [InlineKeyboardButton(text="👥 Партнери", callback_data="partners"),
         InlineKeyboardButton(text="💰 Виплата", callback_data="payout")]
    ])
def money(v): return f"{Decimal(v):,.2f}".replace(",", " ").replace(".", ",")

@dp.message(CommandStart())
async def start(m: Message):
    if not allowed(m.from_user.id):
        await m.answer("⛔ Доступ закритий. Ваш Telegram ID: <code>%s</code>" % m.from_user.id); return
    await m.answer("💼 <b>SOYER ACCOUNTING</b>\n\nВідкрийте вебзастосунок для роботи з обмінами, витратами та виплатами:\n" + (f"<a href=\"{APP_URL}\">Відкрити SOYER ACCOUNTING</a>" if APP_URL else "Посилання на застосунок ще не налаштоване.") + "\n\nЦей бот надсилатиме повідомлення про нові операції.", reply_markup=menu())

@dp.message(Command("id"))
async def get_id(m: Message):
    await m.answer(f"Ваш Telegram ID: <code>{m.from_user.id}</code>")

@dp.callback_query(F.data == "stats")
async def stats(c: CallbackQuery):
    if not allowed(c.from_user.id): return await c.answer("Немає доступу", show_alert=True)
    d = dashboard()
    await c.message.edit_text(
        "📊 <b>SOYER ACCOUNTING</b>\n\n"
        f"Успішних обмінів: <b>{d['count']}</b>\n"
        f"Валовий прибуток: <b>{money(d['gross'])} {d['currency']}</b>\n"
        f"Збитки: <b>{money(d['losses'])} {d['currency']}</b>\n"
        f"Денис · 80%: <b>{money(d['denis'])} {d['currency']}</b>\n"
        f"Єгор · 20%: <b>{money(d['egor'])} {d['currency']}</b>\n"
        f"Витрати: <b>{money(d['expenses'])} {d['currency']}</b>\n"
        f"Виплачено: <b>{money(d['paid_out'])} {d['currency']}</b>",
        reply_markup=menu())
    await c.answer()

@dp.callback_query(F.data == "exchange")
async def exchange(c: CallbackQuery, state: FSMContext):
    if not allowed(c.from_user.id): return await c.answer("Немає доступу", show_alert=True)
    await state.set_state(ExchangeForm.direction)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="USDT → UAH", callback_data="dir:USDT_UAH")],
        [InlineKeyboardButton(text="UAH → USDT", callback_data="dir:UAH_USDT")]])
    await c.message.answer("Оберіть напрямок:", reply_markup=kb); await c.answer()

@dp.callback_query(F.data.startswith("dir:"))
async def direction(c: CallbackQuery, state: FSMContext):
    if not allowed(c.from_user.id): return await c.answer("Немає доступу", show_alert=True)
    await state.update_data(direction=c.data.split(":")[1])
    await state.set_state(ExchangeForm.received)
    await c.message.answer("Вкажіть суму, отриману від клієнта (число):")
    await c.answer()

@dp.message(ExchangeForm.received)
async def received(m: Message, state: FSMContext):
    try: v = Decimal(m.text.replace(",", "."))
    except: return await m.answer("Введіть коректне число, наприклад 100 або 100.50")
    if not v.is_finite() or v <= 0: return await m.answer("Сума має бути скінченним числом, більшим за нуль.")
    await state.update_data(received=str(v)); await state.set_state(ExchangeForm.paid)
    await m.answer("Вкажіть суму, видану клієнту:")

@dp.message(ExchangeForm.paid)
async def paid(m: Message, state: FSMContext):
    try: v = Decimal(m.text.replace(",", "."))
    except: return await m.answer("Введіть коректне число.")
    if not v.is_finite() or v <= 0: return await m.answer("Сума має бути скінченним числом, більшим за нуль.")
    await state.update_data(paid=str(v)); await state.set_state(ExchangeForm.cost)
    await m.answer("Вкажіть собівартість у валюті видачі. Для точного розрахунку валовий прибуток буде в валюті видачі:")

@dp.message(ExchangeForm.cost)
async def cost(m: Message, state: FSMContext):
    try: v = Decimal(m.text.replace(",", "."))
    except: return await m.answer("Введіть коректне число.")
    if not v.is_finite() or v < 0: return await m.answer("Собівартість має бути скінченним невід'ємним числом.")
    await state.update_data(cost=str(v)); await state.set_state(ExchangeForm.note)
    await m.answer("Примітка або номер заявки (або напишіть —):")

@dp.message(ExchangeForm.note)
async def note(m: Message, state: FSMContext):
    data = await state.get_data()
    try:
        result = add_exchange(data["direction"], Decimal(data["received"]), Decimal(data["paid"]),
                              Decimal(data["cost"]), m.text[:300], m.from_user.id)
    except Exception:
        logging.exception("exchange save failed")
        await m.answer("Не вдалося зберегти операцію. Перевірте підключення до бази даних або введені дані."); await state.clear(); return
    await state.clear()
    gross = result["gross"]
    if gross < 0:
        result_line = f"Збиток: <b>{money(abs(gross))} {result['currency']}</b>"
        shares_line = "Частки партнерів не нараховані через збиток."
    else:
        result_line = f"Валовий прибуток: <b>{money(gross)} {result['currency']}</b>"
        shares_line = (f"Денис (80%): <b>{money(result['denis'])} {result['currency']}</b>\n"
                       f"Єгор (20%): <b>{money(result['egor'])} {result['currency']}</b>")
    await m.answer(
        f"✅ <b>Обмін #{result['id']} записано</b>\n"
        f"{result_line}\n"
        f"{shares_line}\n"
        "Статус: успішний. Частки нараховані, але не виплачені (якщо є прибуток).", reply_markup=menu())

@dp.callback_query(F.data == "history")
async def history(c: CallbackQuery):
    if not allowed(c.from_user.id): return await c.answer("Немає доступу", show_alert=True)
    rows = recent_exchanges()
    if not rows: msg = "📋 Операцій поки немає."
    else:
        msg = "📋 <b>Останні обміни</b>\n\n" + "\n".join(
            f"#{r['id']} · {r['direction']} · {'Збиток' if r['gross'] < 0 else 'Прибуток'} {money(abs(r['gross']))} {r['currency']} · {r['created_at']:%d.%m %H:%M}"
            for r in rows)
    await c.message.edit_text(msg, reply_markup=menu()); await c.answer()

@dp.callback_query(F.data == "partners")
async def partners(c: CallbackQuery):
    if not allowed(c.from_user.id): return await c.answer("Немає доступу", show_alert=True)
    p = partner_report()
    await c.message.edit_text(
        "👥 <b>Партнери</b>\n\n"
        f"Денис · 80%\nНараховано: <b>{money(p['denis_accrued'])}</b>\nВиплачено: <b>{money(p['denis_paid'])}</b>\nДо виплати: <b>{money(p['denis_due'])}</b>\n\n"
        f"Єгор · 20%\nНараховано: <b>{money(p['egor_accrued'])}</b>\nВиплачено: <b>{money(p['egor_paid'])}</b>\nДо виплати: <b>{money(p['egor_due'])}</b>",
        reply_markup=menu()); await c.answer()

@dp.callback_query(F.data == "expense")
async def expense(c: CallbackQuery, state: FSMContext):
    if not allowed(c.from_user.id): return await c.answer("Немає доступу", show_alert=True)
    await state.set_state(ExpenseForm.currency)
    await c.message.answer("Вкажіть валюту витрати (UAH або USDT):"); await c.answer()

@dp.message(ExpenseForm.currency)
async def exp_currency(m: Message, state: FSMContext):
    cur=m.text.strip().upper()
    if cur not in ("UAH","USDT"): return await m.answer("Доступні UAH або USDT.")
    await state.update_data(currency=cur); await state.set_state(ExpenseForm.amount)
    await m.answer("Сума витрати:")

@dp.message(ExpenseForm.amount)
async def exp_amount(m: Message, state: FSMContext):
    try: v=Decimal(m.text.replace(",", "."))
    except: return await m.answer("Введіть число.")
    if not v.is_finite() or v <= 0: return await m.answer("Сума має бути скінченним числом, більшою за нуль.")
    await state.update_data(amount=str(v)); await state.set_state(ExpenseForm.description)
    await m.answer("Опис витрати:")

@dp.message(ExpenseForm.description)
async def exp_desc(m: Message, state: FSMContext):
    d=await state.get_data()
    try:
        add_expense(d["currency"], Decimal(d["amount"]), m.text[:300], m.from_user.id)
    except Exception:
        logging.exception("expense save failed")
        await m.answer("Не вдалося зберегти витрату. Перевірте базу даних.")
        await state.clear(); return
    await state.clear(); await m.answer("✅ Витрату записано.", reply_markup=menu())

@dp.callback_query(F.data == "payout")
async def payout(c: CallbackQuery):
    if not allowed(c.from_user.id): return await c.answer("Немає доступу", show_alert=True)
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Денис", callback_data="pay:denis"),
         InlineKeyboardButton(text="Єгор", callback_data="pay:egor")]])
    await c.message.answer("Кому зафіксувати виплату?", reply_markup=kb); await c.answer()

@dp.callback_query(F.data.startswith("pay:"))
async def payout_person(c: CallbackQuery):
    if not allowed(c.from_user.id): return await c.answer("Немає доступу", show_alert=True)
    await c.message.answer("Для виплати використовуйте команду:\n/pay denis 100 UAH або /pay egor 50 USDT")
    await c.answer()

@dp.message(Command("pay"))
async def payout_cmd(m: Message):
    if not allowed(m.from_user.id): return
    parts=(m.text or "").split()
    if len(parts)!=4 or parts[1].lower() not in ("denis","egor") or parts[3].upper() not in ("UAH","USDT"):
        return await m.answer("Формат: /pay denis 100 UAH або /pay egor 50 USDT")
    try: amount=Decimal(parts[2].replace(",", "."))
    except: return await m.answer("Некоректна сума.")
    if not amount.is_finite() or amount<=0: return await m.answer("Сума має бути скінченним числом, більшою за нуль.")
    add_payout(parts[1].lower(), amount, parts[3].upper(), m.from_user.id)
    await m.answer("✅ Виплату зафіксовано.", reply_markup=menu())

async def api_exchange(request):
    if not API_SECRET: return web.json_response({"error":"API_SECRET not configured"}, status=503)
    raw=await request.read()
    signature=request.headers.get("X-Soyer-Signature","")
    expected=hmac.new(API_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return web.json_response({"error":"unauthorized"}, status=401)
    try: data=json.loads(raw)
    except: return web.json_response({"error":"invalid json"}, status=400)
    required=("external_id","direction","received","paid","cost")
    if any(k not in data for k in required): return web.json_response({"error":"missing fields"}, status=400)
    if not str(data.get("external_id","")).strip(): return web.json_response({"error":"external_id must be non-empty"},status=400)
    try:
        result=add_exchange(data["direction"], Decimal(str(data["received"])), Decimal(str(data["paid"])),
                            Decimal(str(data["cost"])), str(data.get("note",""))[:300], 0,
                            external_id=str(data["external_id"]))
    except ValueError as e:
        return web.json_response({"error":str(e)}, status=409)
    return web.json_response({"ok":True,"exchange_id":result["id"],"gross":str(result["gross"]),
                              "denis":str(result["denis"]),"egor":str(result["egor"])})

async def notify_admins(message):
    for uid in ADMIN_IDS:
        try: await bot.send_message(uid, message)
        except Exception: logging.exception("Telegram notification failed for %s", uid)

async def run_api():
    app=create_app(notify_callback=notify_admins)
    app.router.add_post("/api/exchanges/success", api_exchange)
    runner=web.AppRunner(app); await runner.setup()
    site=web.TCPSite(runner,"0.0.0.0",PORT); await site.start()
    logging.info("API listening on %s", PORT)
    return runner

async def main():
    if not TOKEN: raise RuntimeError("BOT_TOKEN is missing")
    if not ADMIN_IDS or not DENIS_ID or not EGOR_ID: raise RuntimeError("Set ADMIN_IDS, DENIS_ID and EGOR_ID")
    if DENIS_ID == EGOR_ID: raise RuntimeError("DENIS_ID and EGOR_ID must be different")
    init_db()
    runner=await run_api()
    try: await dp.start_polling(bot)
    finally: await runner.cleanup(); await bot.session.close()

if __name__ == "__main__": asyncio.run(main())
