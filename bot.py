# bot.py — ТЕЛЕГРАМ БОТ (asyncpg + Supabase)
import asyncio
import pathlib
from datetime import datetime, timedelta
from aiogram.types import WebAppInfo
import httpx
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, FSInputFile
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from config import BOT_TOKEN, GROQ_API_KEY, ADMIN_ID, SHEET_BEST_URL, SHEET_BEST_KEY, WEBAPP_URL, DATABASE_URL
from database import init_db, get_db

# Инициализация бота
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
SCRIPT_DIR = pathlib.Path(__file__).parent.resolve()

# ===================== AI =====================
async def ask_groq(message: str) -> str:
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "llama-3.1-8b-instant",
        "messages": [
            {"role": "system", "content": "Ты — дружелюбный консультант проката спортивного инвентаря. Отвечай коротко и по-русски."},
            {"role": "user", "content": message}
        ],
        "temperature": 0.7, "max_tokens": 512, "stream": False
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(url, json=payload, headers=headers)
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"].strip()
            return "Сервер перегружен. Попробуй позже."
    except:
        return "Ошибка соединения."

# ===================== КЛАВИАТУРЫ =====================
def main_menu(user_id: int = None):
    keyboard = [
        [KeyboardButton(text="Каталог")],
        [KeyboardButton(text="Мои брони"), KeyboardButton(text="Чат с консультантом")],
        [KeyboardButton(
            text="📊 Открыть панель управления",
            web_app=WebAppInfo(url=f"{WEBAPP_URL}?user_id={user_id}")
        )]
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

def back_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Назад")]], resize_keyboard=True)

def cancel_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Отмена")]], resize_keyboard=True)

def rent_type_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="По часам"), KeyboardButton(text="По дням")],
        [KeyboardButton(text="Отмена")]
    ], resize_keyboard=True)

# ===================== СОСТОЯНИЯ =====================
class States(StatesGroup):
    catalog = State()
    item_view = State()
    rent_type = State()
    booking_date = State()
    booking_time = State()
    booking_duration = State()
    booking_qty = State()
    chat = State()
    my_bookings = State()
    booking_detail = State()

# ===================== УТИЛИТЫ =====================
async def clean_and_send(chat_id: int, text: str, reply_markup=None, state: FSMContext = None, delete_msg: types.Message = None, photo=None):
    """Очистка чата и отправка сообщения"""
    if state:
        data = await state.get_data()
        old_id = data.get('last_msg_id')
        if old_id:
            try:
                await bot.delete_message(chat_id, old_id)
            except:
                pass
    
    if delete_msg:
        try:
            await delete_msg.delete()
        except:
            pass
    
    if photo:
        sent = await bot.send_photo(chat_id, photo=photo, caption=text, reply_markup=reply_markup)
    else:
        sent = await bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode="HTML")
    
    if state:
        await state.update_data(last_msg_id=sent.message_id)
    return sent

# ===================== КАТАЛОГ =====================
async def catalog_menu():
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, name, sport, available_quantity, price_per_hour, price_per_day FROM inventory WHERE available_quantity > 0 ORDER BY id"
        )

    emoji_map = {"футбол": "⚽", "теннис": "🎾", "баскетбол": "🏀", "вело": "🚲", "хоккей": "🏒", "скейт": "🛹", "ролики": "🛼", "фитнес": "🏋️"}
    
    buttons = []
    catalog_menu.current_items = []
    for row in rows:
        emoji = next((v for k, v in emoji_map.items() if k in row['sport'].lower()), "🎽")
        btn_text = f"{emoji} {row['name']} ({row['available_quantity']} шт.)"
        buttons.append([KeyboardButton(text=btn_text)])
        catalog_menu.current_items.append({
            "text": btn_text, "id": row['id'], "name": row['name'], 
            "sport": row['sport'], "qty": row['available_quantity'], 
            "price_per_hour": row['price_per_hour'], "price_per_day": row['price_per_day']
        })
    buttons.append([KeyboardButton(text="Назад")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

catalog_menu.current_items = []

# ===================== ЛОГИРОВАНИЕ =====================
async def log_to_sheet(full_name: str, username: str, user_id: int, item_name: str, action: str,
                       booking_date: str = None, booking_time: str = None, total_price: float = 0):
    date_time_str = f"{booking_date} {booking_time}" if action == "book" and booking_date and booking_time else datetime.now().strftime("%d.%m.%Y %H:%M")
    row = {
        "Дата/время": date_time_str,
        "Действие": "БРОНЬ" if action == "book" else "ВОЗВРАТ",
        "Имя": full_name,
        "Username": username or "-",
        "ID": str(user_id),
        "Товар": item_name,
        "Сумма": f"{total_price:.0f}₸" if total_price else "-"
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            await client.post(SHEET_BEST_URL, json=[row], headers={"X-Api-Key": SHEET_BEST_KEY.strip(), "Content-Type": "application/json"})
    except Exception as e:
        print("Ошибка записи в таблицу:", e)

# ===================== НАПОМИНАЛКИ =====================
async def reminder_worker():
    """Фоновая задача для напоминаний"""
    while True:
        try:
            now = datetime.now()
            reminder_time = now + timedelta(minutes=30)
            
            pool = await get_db()
            async with pool.acquire() as conn:
                rows = await conn.fetch('''
                    SELECT b.id, b.user_id, b.quantity, i.name, b.return_datetime, 
                           b.rent_type, b.duration, b.total_price
                    FROM bookings b 
                    JOIN inventory i ON b.item_id = i.id 
                    WHERE b.returned = 0 AND b.reminder_sent = 0
                    AND b.return_datetime <= $1 AND b.return_datetime > $2
                ''', reminder_time.isoformat(), now.isoformat())
                
                for row in rows:
                    return_time = datetime.fromisoformat(row['return_datetime']).strftime("%H:%M %d.%m.%Y")
                    
                    await bot.send_message(row['user_id'],
                        f"⏰ <b>Напоминание!</b>\n\n"
                        f"Через 30 минут нужно вернуть:\n"
                        f"<b>{row['name']} ×{row['quantity']}</b>\n"
                        f"Время возврата: <b>{return_time}</b>\n"
                        f"Стоимость: <b>{row['total_price']:.0f}₸</b>"
                    )
                    
                    await conn.execute("UPDATE bookings SET reminder_sent = 1 WHERE id = $1", row['id'])
                    
                    await bot.send_message(ADMIN_ID,
                        f"🔔 Напоминание отправлено ID:{row['user_id']}\n"
                        f"Товар: {row['name']} ×{row['quantity']}, возврат в {return_time}"
                    )
                        
        except Exception as e:
            print(f"Ошибка напоминаний: {e}")
        
        await asyncio.sleep(60)

# ===================== ХЕНДЛЕРЫ =====================
@dp.message(CommandStart())
async def start(m: types.Message, state: FSMContext):
    await state.clear()
    await clean_and_send(m.chat.id, "👋 Добро пожаловать!\n\nЧем могу помочь?", 
                        reply_markup=main_menu(user_id=m.from_user.id), state=state, delete_msg=m)

@dp.message(F.text == "Каталог")
async def show_catalog(m: types.Message, state: FSMContext):
    await state.set_state(States.catalog)
    await clean_and_send(m.chat.id, "Выберите товар:", 
                        reply_markup=await catalog_menu(), state=state, delete_msg=m)

@dp.message(F.text == "Назад")
async def back_to_main(m: types.Message, state: FSMContext):
    await state.clear()
    await clean_and_send(m.chat.id, "Главное меню", 
                        reply_markup=main_menu(), state=state, delete_msg=m)

@dp.message(F.text == "Мои брони")
async def my_bookings_main(m: types.Message, state: FSMContext):
    await state.clear()
    
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT b.id, b.quantity, i.name, b.booking_date, b.booking_time,
                   b.rent_type, b.duration, b.total_price, b.return_datetime
            FROM bookings b JOIN inventory i ON b.item_id = i.id
            WHERE b.user_id = $1 AND b.returned = 0 ORDER BY b.booked_at DESC
        ''', m.from_user.id)

    if not rows:
        await clean_and_send(m.chat.id, "У вас нет активных броней", 
                            reply_markup=main_menu(), state=state, delete_msg=m)
        return

    total_sum = sum(row['total_price'] for row in rows)
    message_text = "<b>Ваши активные брони:</b>\n\n"
    
    for idx, row in enumerate(rows, 1):
        return_time = datetime.fromisoformat(row['return_datetime']).strftime("%H:%M %d.%m.%Y")
        rent_text = f"{row['duration']} ч." if row['rent_type'] == 'hour' else f"{row['duration']} дн."
        message_text += (
            f"{idx}. <b>{row['name']}</b> ×{row['quantity']}\n"
            f"   Тип: {rent_text}\n"
            f"   Стоимость: <b>{row['total_price']:.0f}₸</b>\n"
            f"   Возврат до: <b>{return_time}</b>\n\n"
        )
    
    message_text += f"<b>Общая сумма: {total_sum:.0f}₸</b>\n\nВыберите бронь для возврата:"

    await state.update_data(my_bookings_list=[dict(row) for row in rows])
    buttons = [[KeyboardButton(text=f"{row['quantity']}× {row['name']} — {row['total_price']:.0f}₸")] for row in rows]
    buttons.append([KeyboardButton(text="Назад")])
    
    await clean_and_send(m.chat.id, message_text,
                        reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True),
                        state=state, delete_msg=m)
    await state.set_state(States.my_bookings)

@dp.message(States.my_bookings)
async def booking_selected(m: types.Message, state: FSMContext):
    if m.text == "Назад":
        await back_to_main(m, state)
        return

    data = await state.get_data()
    bookings = data.get("my_bookings_list", [])
    
    selected = next((b for b in bookings if f"{b['quantity']}× {b['name']} — {b['total_price']:.0f}₸" in m.text), None)
    if not selected:
        return

    return_time = datetime.fromisoformat(selected['return_datetime']).strftime("%H:%M %d.%m.%Y")
    rent_text = f"{selected['duration']} ч." if selected['rent_type'] == 'hour' else f"{selected['duration']} дн."
    
    await state.update_data(selected_booking_id=selected['id'], selected_item_name=selected['name'], 
                           selected_qty=selected['quantity'], selected_price=selected['total_price'])
    await state.set_state(States.booking_detail)

    kb = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Вернуть")],
        [KeyboardButton(text="Назад")]
    ], resize_keyboard=True)

    await clean_and_send(m.chat.id,
        f"<b>Детали брони:</b>\n\n"
        f"Товар: <b>{selected['name']}</b> ×{selected['quantity']}\n"
        f"Тип аренды: {rent_text}\n"
        f"Стоимость: <b>{selected['total_price']:.0f}₸</b>\n"
        f"Время возврата: <b>{return_time}</b>\n\n"
        f"Нажмите «Вернуть» для подтверждения",
        reply_markup=kb, state=state, delete_msg=m)

@dp.message(F.text == "Вернуть")
async def return_booking(m: types.Message, state: FSMContext):
    data = await state.get_data()
    booking_id = data.get("selected_booking_id")
    
    if not booking_id:
        await clean_and_send(m.chat.id, "Ошибка. Начните заново.",
                            reply_markup=main_menu(), state=state, delete_msg=m)
        return

    pool = await get_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow('''
            SELECT b.quantity, i.name, i.id, b.total_price, b.return_datetime, b.rent_type, b.duration
            FROM bookings b JOIN inventory i ON b.item_id = i.id 
            WHERE b.id = $1 AND b.user_id = $2 AND b.returned = 0
        ''', booking_id, m.from_user.id)
        
        if not row:
            await clean_and_send(m.chat.id, "Бронь уже удалена или возвращена.",
                                reply_markup=main_menu(), state=state, delete_msg=m)
            return

        quantity, item_name, item_id, price, return_dt, rtype, duration = row['quantity'], row['name'], row['id'], row['total_price'], row['return_datetime'], row['rent_type'], row['duration']
        return_time = datetime.fromisoformat(return_dt)
        
        now = datetime.now()
        is_overdue = now > return_time
        overdue_text = ""
        
        if is_overdue:
            overdue_minutes = int((now - return_time).total_seconds() / 60)
            overdue_text = f"\n⚠️ <b>Просрочка: {overdue_minutes} мин.</b>"
        
        await conn.execute("UPDATE inventory SET available_quantity = available_quantity + $1 WHERE id = $2", 
                          quantity, item_id)
        await conn.execute("UPDATE bookings SET returned = 1 WHERE id = $1", booking_id)

    await log_to_sheet(m.from_user.full_name, m.from_user.username or "", 
                      m.from_user.id, f"{item_name} ×{quantity}", "ret", total_price=price)

    admin_text = (
        "<b>ВОЗВРАТ</b>\n\n"
        f"<b>{item_name} ×{quantity} шт.</b>\n"
        f"Стоимость: {price:.0f}₸\n"
        f"{'⚠️ ПРОСРОЧЕННЫЙ' if is_overdue else '✅ Вовремя'}"
        f"{overdue_text}\n\n"
        f"От: {m.from_user.full_name}\n"
        f"@{m.from_user.username or '-'}\n"
        f"ID: <code>{m.from_user.id}</code>\n"
        f"{now.strftime('%d.%m.%Y %H:%M')}"
    )
    await bot.send_message(ADMIN_ID, admin_text, parse_mode="HTML")

    user_text = (
        f"✅ Готово!\n\n"
        f"<b>{item_name}</b> ×{quantity} шт. возвращено.\n"
        f"Стоимость аренды: <b>{price:.0f}₸</b>"
        f"{overdue_text}\n\n"
        f"Спасибо за пользование нашим сервисом!"
    )
    
    await state.clear()
    await clean_and_send(m.chat.id, user_text, reply_markup=main_menu(), state=state, delete_msg=m)

@dp.message(States.catalog)
async def item_selected(m: types.Message, state: FSMContext):
    if m.text == "Назад":
        await back_to_main(m, state)
        return

    selected = next((item for item in catalog_menu.current_items if item["text"] == m.text), None)
    if not selected:
        return

    await state.update_data(
        item_id=selected["id"], item_name=selected["name"], available=selected["qty"],
        price_per_hour=selected["price_per_hour"], price_per_day=selected["price_per_day"]
    )
    await state.set_state(States.item_view)

    photo_path = SCRIPT_DIR / "items" / f"{selected['id']}.jpg"
    photo = FSInputFile(photo_path) if photo_path.exists() else None

    kb = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Забронировать")],
        [KeyboardButton(text="Назад")]
    ], resize_keyboard=True)

    text = (
        f"<b>{selected['name']}</b>\n"
        f"Категория: {selected['sport'].capitalize()}\n"
        f"В наличии: {selected['qty']} шт.\n\n"
        f"Цены:\n"
        f"• Почасовая: <b>{selected['price_per_hour']:.0f}₸/час</b>\n"
        f"• Посуточная: <b>{selected['price_per_day']:.0f}₸/день</b>"
    )
    
    await clean_and_send(m.chat.id, text, reply_markup=kb, state=state, delete_msg=m, photo=photo)

@dp.message(F.text == "Забронировать")
async def booking_start(m: types.Message, state: FSMContext):
    await state.set_state(States.rent_type)
    await clean_and_send(m.chat.id,
        "Выберите тип аренды:\n\n"
        "• <b>По часам</b> — минимум 1 час\n"
        "• <b>По дням</b> — от 1 до 30 дней",
        reply_markup=rent_type_kb(), state=state, delete_msg=m)

@dp.message(States.rent_type)
async def process_rent_type(m: types.Message, state: FSMContext):
    if m.text == "Отмена":
        await state.clear()
        await clean_and_send(m.chat.id, "Отменено", reply_markup=main_menu(), state=state, delete_msg=m)
        return
    
    if m.text not in ["По часам", "По дням"]:
        await m.answer("Выберите тип аренды кнопкой!", reply_markup=rent_type_kb())
        return
    
    rent_type = 'hour' if m.text == "По часам" else 'day'
    await state.update_data(rent_type=rent_type)
    await state.set_state(States.booking_date)
    
    type_text = "часовой аренды" if rent_type == 'hour' else "дневной аренды"
    await clean_and_send(m.chat.id, f"Введите дату начала {type_text} (ДД.ММ.ГГГГ):",
                        reply_markup=cancel_kb(), state=state, delete_msg=m)

@dp.message(States.booking_date)
async def booking_date(m: types.Message, state: FSMContext):
    if m.text in ["Отмена", "Назад"]:
        await state.clear()
        await clean_and_send(m.chat.id, "Отменено", reply_markup=main_menu(), state=state, delete_msg=m)
        return

    text = m.text.strip()
    if len(text) == 5 and text[2] == ".":
        text += f".{datetime.now().year}"
    
    try:
        date_obj = datetime.strptime(text, "%d.%m.%Y")
        if date_obj.date() < datetime.now().date():
            await m.answer("Дата не может быть в прошлом!", reply_markup=cancel_kb())
            return
    except ValueError:
        await m.answer("Неверный формат!\nПример: <b>15.12.2025</b>", reply_markup=cancel_kb())
        return

    await state.update_data(date=text, date_obj=date_obj)
    data = await state.get_data()
    
    if data['rent_type'] == 'hour':
        await state.set_state(States.booking_time)
        await clean_and_send(m.chat.id, "Укажите время начала (ЧЧ:ММ):",
                            reply_markup=cancel_kb(), state=state, delete_msg=m)
    else:
        await state.set_state(States.booking_duration)
        await clean_and_send(m.chat.id, "На сколько дней? (1-30):",
                            reply_markup=cancel_kb(), state=state, delete_msg=m)

@dp.message(States.booking_time)
async def booking_time(m: types.Message, state: FSMContext):
    if m.text in ["Отмена", "Назад"]:
        await state.clear()
        await clean_and_send(m.chat.id, "Отменено", reply_markup=main_menu(), state=state, delete_msg=m)
        return

    text = m.text.strip()
    if len(text) != 5 or text[2] != ":" or not text[:2].isdigit() or not text[3:].isdigit():
        await m.answer("Неверный формат!\nПример: <b>14:00</b>", reply_markup=cancel_kb())
        return
    
    hour, minute = int(text[:2]), int(text[3:])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        await m.answer("Неверное время!", reply_markup=cancel_kb())
        return

    data = await state.get_data()
    date_obj = data.get('date_obj')
    if date_obj.date() == datetime.now().date():
        now_time = datetime.now().time()
        if hour < now_time.hour or (hour == now_time.hour and minute < now_time.minute):
            await m.answer("Время не может быть в прошлом!", reply_markup=cancel_kb())
            return

    await state.update_data(time=text, start_hour=hour, start_minute=minute)
    await state.set_state(States.booking_duration)
    await clean_and_send(m.chat.id, "На сколько часов? (минимум 1):",
                        reply_markup=cancel_kb(), state=state, delete_msg=m)

@dp.message(States.booking_duration)
async def booking_duration(m: types.Message, state: FSMContext):
    if m.text == "Отмена":
        await state.clear()
        await clean_and_send(m.chat.id, "Отменено", reply_markup=main_menu(), state=state, delete_msg=m)
        return
    
    try:
        duration = int(m.text)
    except:
        await m.answer("Введите число!")
        return
    
    if duration < 1:
        await m.answer("Минимум 1!")
        return
    
    data = await state.get_data()
    rent_type = data['rent_type']
    
    if rent_type == 'day' and duration > 30:
        await m.answer("Максимум 30 дней!")
        return
    
    date_obj = data['date_obj']
    
    if rent_type == 'hour':
        start_hour = data.get('start_hour', 0)
        start_minute = data.get('start_minute', 0)
        start_datetime = date_obj.replace(hour=start_hour, minute=start_minute)
        return_datetime = start_datetime + timedelta(hours=duration)
    else:
        start_datetime = date_obj
        return_datetime = date_obj + timedelta(days=duration)
    
    price_per_unit = data['price_per_hour'] if rent_type == 'hour' else data['price_per_day']
    total_price = price_per_unit * duration
    
    await state.update_data(duration=duration, return_datetime=return_datetime.isoformat(), total_price=total_price)
    await state.set_state(States.booking_qty)

    avail = data.get("available", 1)
    rows = [[]]
    for i in range(1, min(avail, 10) + 1):
        rows[-1].append(KeyboardButton(text=str(i)))
        if len(rows[-1]) == 5:
            rows.append([])
    if not rows[-1]:
        rows.pop()
    rows.append([KeyboardButton(text="Отмена")])

    price_text = f"{price_per_unit:.0f}₸/час" if rent_type == 'hour' else f"{price_per_unit:.0f}₸/день"
    return_time_str = return_datetime.strftime("%H:%M %d.%m.%Y") if rent_type == 'hour' else return_datetime.strftime("%d.%m.%Y")
    
    await clean_and_send(m.chat.id,
        f"<b>Предварительный расчет:</b>\n"
        f"Длительность: {duration} {'час.' if rent_type == 'hour' else 'дн.'}\n"
        f"Цена: {price_text}\n"
        f"Возврат до: <b>{return_time_str}</b>\n"
        f"<b>Итого: {total_price:.0f}₸</b>\n\n"
        f"Сколько штук забронировать? (доступно: {avail})",
        reply_markup=ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True),
        state=state, delete_msg=m)

@dp.message(States.booking_qty)
async def booking_confirm(m: types.Message, state: FSMContext):
    if m.text == "Отмена":
        await state.clear()
        await clean_and_send(m.chat.id, "Отменено", reply_markup=main_menu(), state=state, delete_msg=m)
        return
    
    try:
        qty = int(m.text)
    except:
        await m.answer("Нажмите на кнопку с числом!")
        return

    if qty < 1:
        await m.answer("Минимум 1!")
        return

    data = await state.get_data()
    
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            avail = await conn.fetchval("SELECT available_quantity FROM inventory WHERE id=$1", data["item_id"])
            
            if avail < qty:
                await clean_and_send(m.chat.id, "Кто-то успел раньше!",
                                    reply_markup=main_menu(), state=state, delete_msg=m)
                return

            total_price = data['total_price'] * qty
            
            await conn.execute('UPDATE inventory SET available_quantity = available_quantity - $1 WHERE id=$2',
                            qty, data["item_id"])
            
            # ИСПРАВЛЕНО: убраны скобки вокруг аргументов, передаём через запятую
            await conn.execute('''
                INSERT INTO bookings 
                (user_id, item_id, quantity, rent_type, booking_date, booking_time, 
                 duration, return_datetime, total_price, booked_at, reminder_sent, returned) 
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            ''', 
            m.from_user.id,           # $1
            data["item_id"],          # $2
            qty,                      # $3
            data['rent_type'],        # $4
            data['date'],             # $5
            data.get('time', '00:00'), # $6
            data['duration'],         # $7
            data['return_datetime'],  # $8
            total_price,              # $9
            datetime.now().strftime("%d.%m.%Y %H:%M"), # $10
            0,                        # $11
            0                         # $12
            )

    await log_to_sheet(m.from_user.full_name, m.from_user.username, m.from_user.id,
                      f"{data['item_name']} ×{qty}", "book", data['date'], 
                      data.get('time', '00:00'), total_price)

    return_dt = datetime.fromisoformat(data['return_datetime'])
    return_str = return_dt.strftime("%H:%M %d.%m.%Y") if data['rent_type'] == 'hour' else return_dt.strftime("%d.%m.%Y")
    rent_text = f"{data['duration']} ч." if data['rent_type'] == 'hour' else f"{data['duration']} дн."

    await bot.send_message(ADMIN_ID,
        "<b>НОВАЯ БРОНЬ</b>\n\n"
        f"<b>{data['item_name']} ×{qty} шт.</b>\n"
        f"Тип: {rent_text}\n"
        f"<b>Сумма: {total_price:.0f}₸</b>\n"
        f"Начало: {data['date']} {data.get('time', '')}\n"
        f"Возврат до: {return_str}\n\n"
        f"От: {m.from_user.full_name}\n"
        f"@{m.from_user.username or '-'}\n"
        f"ID: <code>{m.from_user.id}</code>",
        parse_mode="HTML")

    await state.clear()
    await clean_and_send(m.chat.id,
        f"✅ <b>Забронировано!</b>\n\n"
        f"<b>{data['item_name']}</b> ×{qty}\n"
        f"Длительность: {rent_text}\n"
        f"Стоимость: <b>{total_price:.0f}₸</b>\n"
        f"Возврат до: <b>{return_str}</b>\n\n"
        f"За 30 минут до возврата пришлю напоминание.\n"
        f"Спасибо!",
        reply_markup=main_menu(), state=state, delete_msg=m)

@dp.message(F.text == "Чат с консультантом")
async def start_chat(m: types.Message, state: FSMContext):
    await state.set_state(States.chat)
    await clean_and_send(m.chat.id, "Слушаю вас…\n\nНапишите вопрос или нажмите «Назад»",
                        reply_markup=back_kb(), state=state, delete_msg=m)

@dp.message(States.chat)
async def chat_handler(m: types.Message, state: FSMContext):
    if m.text == "Назад":
        await back_to_main(m, state)
        return

    await bot.send_chat_action(m.chat.id, "typing")
    answer = await ask_groq(m.text)
    
    await clean_and_send(m.chat.id,
        f"<b>Вопрос:</b> {m.text}\n\n"
        f"<b>Ответ:</b> {answer}\n\n"
        f"Еще вопрос или «Назад»",
        reply_markup=back_kb(), state=state, delete_msg=m)

# ===================== ЗАПУСК БОТА =====================
async def start_bot():
    await init_db()
    print("🤖 Бот запущен")
    print(f"🌐 Web App: {WEBAPP_URL}")
    
    # Запускаем напоминания
    asyncio.create_task(reminder_worker())
    
    # Запускаем поллинг
    await dp.start_polling(bot)
    # В конец bot.py добавь:
if __name__ == "__main__":

    asyncio.run(start_bot())



