# database.py — Neon Database для бота и веба
import os
import asyncpg
import psycopg2
from psycopg2.extras import RealDictCursor

# Очищаем URL от channel_binding
DATABASE_URL = os.getenv('DATABASE_URL', '')
if 'channel_binding' in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.split('&channel_binding')[0]

# ===================== ASYNC (для Telegram бота) =====================

_pool = None

async def get_pool():
    """Получить пул соединений"""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    return _pool

async def init_db():
    """Инициализация базы данных"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Создаём таблицы
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS inventory (
                id SERIAL PRIMARY KEY,
                name TEXT UNIQUE,
                sport TEXT,
                total_quantity INTEGER,
                available_quantity INTEGER,
                price_per_hour REAL DEFAULT 0,
                price_per_day REAL DEFAULT 0
            )
        ''')
        
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS bookings (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                item_id INTEGER,
                quantity INTEGER,
                rent_type TEXT,
                booking_date TEXT,
                booking_time TEXT,
                duration INTEGER,
                return_datetime TEXT,
                total_price REAL,
                booked_at TEXT,
                reminder_sent INTEGER DEFAULT 0,
                returned INTEGER DEFAULT 0
            )
        ''')
        
        # Проверяем, есть ли данные
        count = await conn.fetchval("SELECT COUNT(*) FROM inventory")
        if count == 0:
            items = [
                ("Футбольный мяч", "футбол", 10, 10, 500, 2500),
                ("Теннисная ракетка", "теннис", 8, 8, 750, 4000),
                ("Баскетбольный мяч", "баскетбол", 6, 6, 500, 2500),
                ("Горный велосипед", "вело", 4, 4, 1500, 7500),
                ("Хоккейные коньки", "хоккей", 12, 12, 1000, 5000),
                ("Скейтборд", "скейт", 5, 5, 750, 3500),
                ("Роликовые коньки", "ролики", 15, 15, 750, 3500),
                ("Гантели 10 кг", "фитнес", 20, 20, 250, 1500),
            ]
            await conn.executemany(
                "INSERT INTO inventory (name, sport, total_quantity, available_quantity, price_per_hour, price_per_day) VALUES ($1, $2, $3, $4, $5, $6)",
                items
            )

async def get_db():
    """Получить соединение из пула"""
    pool = await get_pool()
    return pool

# ===================== SYNC (для Flask на Vercel) =====================

def get_db_sync():
    """Синхронное соединение для Flask"""
    conn = psycopg2.connect(DATABASE_URL, sslmode='require')
    conn.cursor_factory = RealDictCursor
    return conn

def init_db_sync():
    """Синхронная инициализация для Flask"""
    conn = psycopg2.connect(DATABASE_URL, sslmode='require')
    c = conn.cursor()
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS inventory (
            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE,
            sport TEXT,
            total_quantity INTEGER,
            available_quantity INTEGER,
            price_per_hour REAL DEFAULT 0,
            price_per_day REAL DEFAULT 0
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS bookings (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            item_id INTEGER,
            quantity INTEGER,
            rent_type TEXT,
            booking_date TEXT,
            booking_time TEXT,
            duration INTEGER,
            return_datetime TEXT,
            total_price REAL,
            booked_at TEXT,
            reminder_sent INTEGER DEFAULT 0,
            returned INTEGER DEFAULT 0
        )
    ''')
    
    c.execute("SELECT COUNT(*) FROM inventory")
    if c.fetchone()[0] == 0:
        items = [
            ("Футбольный мяч", "футбол", 10, 10, 500, 2500),
            ("Теннисная ракетка", "теннис", 8, 8, 750, 4000),
            ("Баскетбольный мяч", "баскетбол", 6, 6, 500, 2500),
            ("Горный велосипед", "вело", 4, 4, 1500, 7500),
            ("Хоккейные коньки", "хоккей", 12, 12, 1000, 5000),
            ("Скейтборд", "скейт", 5, 5, 750, 3500),
            ("Роликовые коньки", "ролики", 15, 15, 750, 3500),
            ("Гантели 10 кг", "фитнес", 20, 20, 250, 1500),
        ]
        c.executemany(
            "INSERT INTO inventory (name, sport, total_quantity, available_quantity, price_per_hour, price_per_day) VALUES (%s, %s, %s, %s, %s, %s)",
            items
        )
        conn.commit()

    conn.close()
    # Добавьте в конец database.py

async def get_user_bookings(user_id: int):
    """Получить брони конкретного пользователя"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT b.id, b.quantity, i.name, b.booking_date, b.booking_time,
                   b.rent_type, b.duration, b.total_price, b.return_datetime, b.returned
            FROM bookings b 
            JOIN inventory i ON b.item_id = i.id
            WHERE b.user_id = $1 
            ORDER BY b.booked_at DESC
        ''', user_id)
        return [dict(row) for row in rows]

async def return_booking_safe(booking_id: int, user_id: int):
    """Вернуть бронь только если она принадлежит пользователю"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Проверяем, что бронь принадлежит этому пользователю
            booking = await conn.fetchrow(
                'SELECT item_id, quantity, returned FROM bookings WHERE id = $1 AND user_id = $2',
                booking_id, user_id
            )
            if not booking:
                return None  # Бронь не найдена или не принадлежит пользователю
            
            if booking['returned']:
                return False  # Уже возвращена
            
            # Обновляем инвентарь
            await conn.execute(
                'UPDATE inventory SET available_quantity = available_quantity + $1 WHERE id = $2',
                booking['quantity'], booking['item_id']
            )
            # Помечаем как возвращенную
            await conn.execute(
                'UPDATE bookings SET returned = 1 WHERE id = $1',
                booking_id
            )
            return True
