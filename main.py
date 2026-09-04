# -*- coding: utf-8 -*-
# ============================================================
#  🐸 POSTFROG — АВТОПОСТИНГ + КОНКУРСЫ + РЕФЕРКА + ОТЧЁТЫ
#  Установка: pip install -U aiogram
# ============================================================
import asyncio
import logging
import random
import re
import sqlite3
import html
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup,
                           InlineKeyboardButton, LabeledPrice, PreCheckoutQuery)
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ======================== НАСТРОЙКИ ========================
BOT_TOKEN = "8859052871:AAHuJBQEY628Z9lX7-TZk7WcTDW2Z8TLdz8"
ADMIN_ID = 6603375763

# ⚠️ ВАЖНО! Если бот запущен на сервере/VPS/Replit и посты выходят
# не вовремя — здесь часовой пояс сервера не совпадает с твоим.
# Москва = +3. Если время бота (см. /debug) отстаёт от твоего на 3 часа —
# поставь здесь 3. Если спешит — поставь отрицательное число.
TZ_OFFSET = 0

DATE_FMT = "%d.%m.%Y %H:%M"
DB_FMT = "%Y-%m-%d %H:%M:%S"
LINE = "🐸━━━━━━━━━━━━━━━━━━🐸"

# ======================== ЛОГИ (файл postfrog.log) ========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("postfrog.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logging.getLogger("aiogram").setLevel(logging.WARNING)
log = logging.getLogger("postfrog")

def now_local():
    """Текущее время с учётом сдвига часового пояса"""
    return datetime.now() + timedelta(hours=TZ_OFFSET)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

BOT_USERNAME = ""
scheduler_last_tick = None
scheduler_task = None
pending_timers = []  # держим ссылки на таймеры, чтобы их не съел сборщик мусора

# ======================== БАЗА ДАННЫХ ========================
db = sqlite3.connect("bot.db", check_same_thread=False, timeout=30)
db.row_factory = sqlite3.Row
cur = db.cursor()

def init_db():
    cur.execute("PRAGMA journal_mode=WAL")
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        sub_type TEXT DEFAULT 'free',
        sub_until TEXT,
        joined TEXT
    );
    CREATE TABLE IF NOT EXISTS promos(
        code TEXT PRIMARY KEY,
        days INTEGER,
        used_by INTEGER
    );
    CREATE TABLE IF NOT EXISTS channels(
        chat_id INTEGER PRIMARY KEY,
        title TEXT,
        owner_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS posts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id INTEGER,
        chat_id INTEGER,
        text TEXT,
        photo TEXT,
        delete_hours INTEGER,
        send_at TEXT,
        message_id INTEGER,
        delete_at TEXT,
        status TEXT DEFAULT 'scheduled'
    );
    CREATE TABLE IF NOT EXISTS giveaways(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id INTEGER,
        channel_id INTEGER,
        description TEXT,
        photo TEXT,
        winners INTEGER,
        sub_required INTEGER DEFAULT 0,
        end_type TEXT,
        end_value TEXT,
        publish_at TEXT,
        status TEXT DEFAULT 'created',
        post_message_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS g_channels(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        giveaway_id INTEGER,
        chat_id INTEGER,
        title TEXT
    );
    CREATE TABLE IF NOT EXISTS participants(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        giveaway_id INTEGER,
        user_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS settings(
        key TEXT PRIMARY KEY,
        value TEXT
    );
    CREATE TABLE IF NOT EXISTS chan_stats(
        chat_id INTEGER,
        date TEXT,
        posts INTEGER DEFAULT 0,
        reactions INTEGER DEFAULT 0,
        PRIMARY KEY(chat_id, date)
    );
    CREATE TABLE IF NOT EXISTS purchases(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        stars INTEGER,
        days INTEGER,
        date TEXT
    );
    """)
    db.commit()
    # ---- миграции старой базы ----
    cols = [r[1] for r in cur.execute("PRAGMA table_info(users)").fetchall()]
    if "referred_by" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER")
        db.commit()
    cols = [r[1] for r in cur.execute("PRAGMA table_info(posts)").fetchall()]
    if "btn_text" not in cols:
        cur.execute("ALTER TABLE posts ADD COLUMN btn_text TEXT")
    if "btn_url" not in cols:
        cur.execute("ALTER TABLE posts ADD COLUMN btn_url TEXT")
    db.commit()
    # ---- восстановление после сбоя: размораживаем "застрявшие" записи ----
    cur.execute("UPDATE posts SET status='scheduled' WHERE status='publishing'")
    cur.execute("UPDATE giveaways SET status='created' WHERE status='publishing'")
    cur.execute("UPDATE giveaways SET status='running' WHERE status='finishing'")
    db.commit()

# ======================== ХЕЛПЕРЫ БД ========================
def get_user(uid):
    cur.execute("SELECT * FROM users WHERE user_id=?", (uid,))
    return cur.fetchone()

def upsert_user(uid, username=None, referred_by=None):
    cur.execute("SELECT user_id FROM users WHERE user_id=?", (uid,))
    if not cur.fetchone():
        cur.execute("INSERT INTO users(user_id, username, joined, referred_by) VALUES(?,?,?,?)",
                    (uid, username, now_local().strftime(DB_FMT), referred_by))
        db.commit()
        return True
    cur.execute("UPDATE users SET username=? WHERE user_id=?", (username, uid))
    db.commit()
    return False

def is_premium_user(u):
    if not u:
        return False
    if u["sub_type"] == "lifetime":
        return True
    if u["sub_type"] == "premium" and u["sub_until"]:
        try:
            return datetime.strptime(u["sub_until"], DB_FMT) > now_local()
        except:
            return False
    return False

def add_sub_days(uid, days):
    u = get_user(uid)
    if not u:
        upsert_user(uid)
        u = get_user(uid)
    if u["sub_type"] == "lifetime":
        return
    if days == 0:
        cur.execute("UPDATE users SET sub_type='lifetime', sub_until=NULL WHERE user_id=?", (uid,))
        db.commit()
        return
    now = now_local()
    base = now
    if u["sub_until"]:
        try:
            old = datetime.strptime(u["sub_until"], DB_FMT)
            if old > now:
                base = old
        except:
            pass
    until = base + timedelta(days=days)
    cur.execute("UPDATE users SET sub_type='premium', sub_until=? WHERE user_id=?",
                (until.strftime(DB_FMT), uid))
    db.commit()

def remove_sub(uid):
    cur.execute("UPDATE users SET sub_type='free', sub_until=NULL WHERE user_id=?", (uid,))
    db.commit()

def sub_text(u):
    if not u:
        return "🆓 Free"
    if u["sub_type"] == "lifetime":
        return "💚 PREMIUM — НАВСЕГДА"
    if u["sub_type"] == "premium" and u["sub_until"]:
        until = datetime.strptime(u["sub_until"], DB_FMT)
        if until > now_local():
            return f"💚 PREMIUM — до {until.strftime('%d.%m.%Y %H:%M')}"
    return "🆓 Free"

def get_setting(key):
    cur.execute("SELECT value FROM settings WHERE key=?", (key,))
    r = cur.fetchone()
    return r["value"] if r else None

def set_setting(key, value):
    cur.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?,?)", (key, str(value)))
    db.commit()

def del_setting(key):
    cur.execute("DELETE FROM settings WHERE key=?", (key,))
    db.commit()

def get_ad_text():
    return get_setting("ad_text")

def get_force_channel():
    cid = get_setting("force_channel_id")
    if cid:
        return int(cid), get_setting("force_channel_title") or "Канал"
    return None, None

def save_channel(chat_id, title, owner_id):
    cur.execute("INSERT OR REPLACE INTO channels(chat_id, title, owner_id) VALUES(?,?,?)",
                (chat_id, title, owner_id))
    db.commit()

def touch_channel(chat_id, title):
    cur.execute("INSERT OR IGNORE INTO channels(chat_id, title, owner_id) VALUES(?,?,NULL)",
                (chat_id, title))
    db.commit()

def user_channels(uid):
    cur.execute("SELECT * FROM channels WHERE owner_id=?", (uid,))
    return cur.fetchall()

def count_user_channels(uid):
    cur.execute("SELECT COUNT(*) c FROM channels WHERE owner_id=?", (uid,))
    return cur.fetchone()["c"]

def count_referrals(uid):
    cur.execute("SELECT COUNT(*) c FROM users WHERE referred_by=?", (uid,))
    return cur.fetchone()["c"]

# ======================== FSM ========================
class AddPost(StatesGroup):
    select_channel = State()
    text = State()
    photo = State()
    btn_text = State()
    btn_url = State()
    autodelete = State()
    autodelete_hours = State()
    when = State()
    schedule_time = State()
    confirm = State()
    edit_text = State()

class Giveaway(StatesGroup):
    description = State()
    photo = State()
    winners = State()
    condition = State()
    add_channel = State()
    select_channel = State()
    end_type = State()
    end_time = State()
    end_count = State()
    publish_when = State()
    publish_time = State()

class PromoIn(StatesGroup):
    code = State()

class ReportFSM(StatesGroup):
    channel = State()

class AdminFSM(StatesGroup):
    promo_code = State()
    promo_days = State()
    give_id = State()
    give_days = State()
    take_id = State()
    ad_text = State()
    broadcast = State()
    broadcast_confirm = State()
    force_channel = State()

# ======================== КЛАВИАТУРЫ ========================
def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Мой профиль", callback_data="profile")],
        [InlineKeyboardButton(text="📝 Добавить пост", callback_data="add_post")],
        [InlineKeyboardButton(text="🎁 Создать конкурс", callback_data="new_giveaway")],
        [InlineKeyboardButton(text="⭐ Подписка", callback_data="subscription"),
         InlineKeyboardButton(text="🎟 Промокод", callback_data="promo")],
        [InlineKeyboardButton(text="📊 Отчёт по каналу", callback_data="report")],
        [InlineKeyboardButton(text="👥 Реферальная система", callback_data="referral")],
        [InlineKeyboardButton(text="🏆 Мои конкурсы", callback_data="my_giveaways")],
    ])

def back_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🐸 В меню", callback_data="menu")]
    ])

def sub_buy_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ 7 дней — 9 ⭐", callback_data="buy_7")],
        [InlineKeyboardButton(text="⭐ 31 день — 29 ⭐", callback_data="buy_31")],
        [InlineKeyboardButton(text="⭐ 93 дня — 49 ⭐", callback_data="buy_93")],
        [InlineKeyboardButton(text="⭐ Навсегда — 99 ⭐", callback_data="buy_life")],
        [InlineKeyboardButton(text="🐸 В меню", callback_data="menu")],
    ])

# ======================== ВСПОМОГАТЕЛЬНОЕ ========================
async def bot_is_admin(chat_id):
    try:
        m = await bot.get_chat_member(chat_id, bot.id)
        return m.status == "administrator"
    except Exception as e:
        log.warning(f"bot_is_admin({chat_id}): {e}")
        return False

async def user_subscribed(chat_id, user_id):
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        return m.status in ("member", "administrator", "creator", "restricted")
    except:
        return False

async def extract_chat(message: Message):
    # aiogram 3.4+: forward_origin; старые версии: forward_from_chat
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        origin = getattr(message, "forward_from_chat", None)
    if origin is not None:
        chat = getattr(origin, "chat", None)
        if chat is None and hasattr(origin, "id") and hasattr(origin, "title"):
            chat = origin  # forward_from_chat — это сразу Chat
        if chat is not None and getattr(chat, "title", None):
            return chat.id, chat.title
    if message.text and message.text.startswith("@"):
        try:
            chat = await bot.get_chat(message.text.strip())
            return chat.id, chat.title
        except:
            return None
    return None

def parse_date(text):
    try:
        return datetime.strptime(text.strip(), DATE_FMT)
    except:
        return None

def is_valid_url(u):
    u = u.strip()
    return bool(re.match(r'^https?://\S+$', u)) or u.startswith("tg://")

async def send_to_channel(chat_id, text, photo=None, btn_text=None, btn_url=None, premium=False):
    if not premium:
        ad = get_ad_text()
        if ad:
            text = text + "\n\n─────────────\n" + ad
    kb = None
    if btn_text and btn_url:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=btn_text, url=btn_url)]
        ])
    try:
        if photo:
            msg = await bot.send_photo(chat_id, photo,
                                       caption=text[:1024] if text else None,
                                       reply_markup=kb)
        else:
            msg = await bot.send_message(chat_id, text[:4096], reply_markup=kb)
        return msg
    except Exception as e:
        log.error(f"Ошибка отправки в {chat_id}: {e}")
        return None

# ======================== 🐸 ПУБЛИКАЦИЯ ПОСТА (с защитой от дублей) ========================
async def publish_post_by_id(post_id, notify=True):
    cur.execute("SELECT * FROM posts WHERE id=?", (post_id,))
    p = cur.fetchone()
    if not p or p["status"] != "scheduled":
        return False

    # атомарно забираем пост себе — двойной публикации не будет
    cur.execute("UPDATE posts SET status='publishing' WHERE id=? AND status='scheduled'", (post_id,))
    db.commit()
    if cur.rowcount == 0:
        return False

    if not p["text"] and not p["photo"]:
        cur.execute("UPDATE posts SET status='failed' WHERE id=?", (post_id,))
        db.commit()
        log.warning(f"Пост #{post_id}: пустой")
        return False

    if not await bot_is_admin(p["chat_id"]):
        cur.execute("UPDATE posts SET status='failed' WHERE id=?", (post_id,))
        db.commit()
        try:
            await bot.send_message(
                p["owner_id"],
                "⚠️ Пост не опубликован!\n\n"
                "🐸 Я не администратор в канале.\n"
                "Добавь меня админом с правом «Публикация сообщений» и создай пост заново."
            )
        except:
            pass
        log.warning(f"Пост #{post_id}: бот не админ в {p['chat_id']}")
        return False

    u = get_user(p["owner_id"])
    prem = is_premium_user(u)

    msg = None
    for attempt in range(3):  # 3 попытки с паузой
        msg = await send_to_channel(p["chat_id"], p["text"] or "", p["photo"],
                                    p["btn_text"], p["btn_url"], premium=prem)
        if msg:
            break
        log.warning(f"Пост #{post_id}: попытка {attempt + 1}/3 не удалась, жду 2 сек.")
        await asyncio.sleep(2)

    now = now_local()
    delete_at = None
    if p["delete_hours"]:
        delete_at = (now + timedelta(hours=p["delete_hours"])).strftime(DB_FMT)

    if msg:
        cur.execute("UPDATE posts SET status='published', message_id=?, delete_at=? WHERE id=?",
                    (msg.message_id, delete_at, post_id))
        db.commit()
        log.info(f"[✓] Пост #{post_id} ОПУБЛИКОВАН в {p['chat_id']}")
        if notify:
            try:
                await bot.send_message(p["owner_id"],
                                       "✅ Запланированный пост опубликован в канале! 🐸💚")
            except:
                pass
        return True

    cur.execute("UPDATE posts SET status='failed' WHERE id=?", (post_id,))
    db.commit()
    try:
        await bot.send_message(
            p["owner_id"],
            "⚠️ Пост не удалось опубликовать (3 попытки).\n"
            "🐸 Проверь, что бот — админ канала с правом публикации."
        )
    except:
        pass
    log.error(f"Пост #{post_id}: не опубликован после 3 попыток")
    return False

# ======================== ⏰ ТОЧНЫЕ ТАЙМЕРЫ ========================
def start_post_timer(post_id, send_dt):
    """Точный таймер: выложит пост ровно в указанную секунду"""
    async def _job():
        delay = (send_dt - now_local()).total_seconds()
        if delay > 0:
            log.info(f"[⏰] Таймер поста #{post_id}: жду {int(delay)} сек.")
            await asyncio.sleep(delay)
        log.info(f"[⏰] ТАЙМЕР ПОСТА #{post_id} СРАБОТАЛ — публикую!")
        await publish_post_by_id(post_id, notify=True)
    t = asyncio.create_task(_job())
    pending_timers.append(t)

def start_giveaway_publish_timer(gid, publish_dt):
    async def _job():
        delay = (publish_dt - now_local()).total_seconds()
        if delay > 0:
            log.info(f"[⏰] Таймер публикации конкурса #{gid}: жду {int(delay)} сек.")
            await asyncio.sleep(delay)
        log.info(f"[⏰] ТАЙМЕР КОНКУРСА #{gid} (публикация) СРАБОТАЛ!")
        await publish_giveaway(gid)
    t = asyncio.create_task(_job())
    pending_timers.append(t)

def start_giveaway_finish_timer(gid, end_dt):
    async def _job():
        delay = (end_dt - now_local()).total_seconds()
        if delay > 0:
            log.info(f"[⏰] Таймер итогов конкурса #{gid}: жду {int(delay)} сек.")
            await asyncio.sleep(delay)
        log.info(f"[⏰] ТАЙМЕР КОНКУРСА #{gid} (итоги) СРАБОТАЛ!")
        await finish_giveaway(gid)
    t = asyncio.create_task(_job())
    pending_timers.append(t)

# ======================== /start ========================
@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext, command: CommandObject):
    await state.clear()
    args = command.args.strip() if command.args else ""

    if args.startswith("ref_"):
        try:
            ref_id = int(args.split("_")[1])
        except:
            ref_id = None
        is_new = upsert_user(message.from_user.id, message.from_user.username)
        if is_new and ref_id and ref_id != message.from_user.id:
            cur.execute("UPDATE users SET referred_by=? WHERE user_id=?",
                        (ref_id, message.from_user.id))
            db.commit()
            add_sub_days(ref_id, 1)
            try:
                await bot.send_message(
                    ref_id,
                    f"{LINE}\n   🎉 НОВЫЙ РЕФЕРАЛ! 🎉\n{LINE}\n\n"
                    "🐸 По твоей ссылке пришёл новый друг!\n"
                    "💚 Тебе начислен +1 день PREMIUM!"
                )
            except:
                pass
        await message.answer(
            f"{LINE}\n   🐸 POSTFROG 🐸\n{LINE}\n\n"
            f"👋 Ква-ква, {message.from_user.first_name}!\n"
            "Я помогу с постами и конкурсами 💚\n\n"
            "👇 Выбирай действие:",
            reply_markup=main_menu()
        )
        return

    upsert_user(message.from_user.id, message.from_user.username)

    if args.startswith("gw_"):
        try:
            gid = int(args.split("_")[1])
            await try_join_giveaway(message.from_user.id, gid)
        except:
            pass
        return

    await message.answer(
        f"{LINE}\n   🐸 POSTFROG 🐸\n{LINE}\n\n"
        f"👋 Ква-ква, {message.from_user.first_name}!\n"
        "Я помогу с постами и конкурсами 💚\n\n"
        "👇 Выбирай действие:",
        reply_markup=main_menu()
    )

@router.message(CommandStart())
async def cmd_start_group(message: Message):
    await message.reply("🐸 Ква! Напиши мне в личку, чтобы пользоваться PostFrog 💚")

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Отменено. Ква!", reply_markup=main_menu())

# ======================== 🩺 /debug — ДИАГНОСТИКА ========================
@router.message(Command("debug"))
@router.message(Command("check"))
async def cmd_debug(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    global scheduler_last_tick
    now = now_local()
    if scheduler_last_tick:
        ago = int((datetime.now() - scheduler_last_tick).total_seconds())
        tick_txt = f"✅ работает (тик {ago} сек. назад)"
    else:
        tick_txt = "❌ НЕ РАБОТАЕТ — перезапусти бота!"

    now_s = now.strftime(DB_FMT)
    cur.execute("SELECT id, send_at, chat_id FROM posts WHERE status='scheduled' ORDER BY send_at LIMIT 10")
    sched = cur.fetchall()
    cur.execute("SELECT id, publish_at, end_type, end_value, channel_id FROM giveaways "
                "WHERE status IN ('created','running') ORDER BY id LIMIT 10")
    gws = cur.fetchall()

    text = (
        f"{LINE}\n   🩺 ДИАГНОСТИКА POSTFROG 🩺\n{LINE}\n\n"
        f"🖥 Время бота: {now.strftime(DATE_FMT)}\n"
        f"⚙️ Сдвиг часового пояса: +{TZ_OFFSET} ч.\n"
        f"⏰ Планировщик: {tick_txt}\n"
        f"🔁 Активных таймеров: {len([t for t in pending_timers if not t.done()])}\n"
    )
    if TZ_OFFSET == 0:
        text += "\n💡 Если время бота выше НЕ совпадает с твоими часами —\n" \
                "поставь в коде TZ_OFFSET = разницу в часах!\n"
    if sched:
        text += f"\n📝 Запланированные посты:\n"
        for s in sched:
            try:
                dt = datetime.strptime(s["send_at"], DB_FMT)
                delta = dt - now
                mins = int(delta.total_seconds() // 60)
                text += f"  🌿 #{s['id']} → {dt.strftime(DATE_FMT)} (через {mins} мин.)\n"
            except:
                text += f"  🌿 #{s['id']} → {s['send_at']} (кривая дата!)\n"
    else:
        text += "\n📝 Запланированных постов нет\n"
    if gws:
        text += f"\n🎁 Активные конкурсы:\n"
        for g in gws:
            st = "публикация" if g["status"] == "created" else "идёт"
            end_txt = g["end_value"] if g["end_type"] == "time" else f"после {g['end_value']} участников"
            text += f"  🌿 #{g['id']} [{st}] итоги: {end_txt}\n"
    else:
        text += "\n🎁 Активных конкурсов нет\n"
    text += LINE
    await message.answer(text)

# ======================== МЕНЮ / ПРОФИЛЬ ========================
@router.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text(
        f"{LINE}\n   🐸 POSTFROG — МЕНЮ 🐸\n{LINE}\n\n👇 Выбирай:",
        reply_markup=main_menu()
    )

@router.callback_query(F.data == "profile")
async def cb_profile(c: CallbackQuery):
    u = get_user(c.from_user.id)
    channels = count_user_channels(c.from_user.id)
    refs = count_referrals(c.from_user.id)
    username = f"📛 Username: @{c.from_user.username}\n" if c.from_user.username else ""
    text = (
        f"{LINE}\n   👤 МОЙ ПРОФИЛЬ\n{LINE}\n\n"
        f"🆔 ID: {c.from_user.id}\n"
        f"{username}"
        f"⭐ Статус: {sub_text(u)}\n"
        f"📡 Каналов/групп: {channels}\n"
        f"👥 Рефералов: {refs}\n"
        f"{LINE}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏆 Мои конкурсы", callback_data="my_giveaways")],
        [InlineKeyboardButton(text="🐸 В меню", callback_data="menu")],
    ])
    await c.message.edit_text(text, reply_markup=kb)

# ======================== РЕФЕРальная СИСТЕМА ========================
@router.callback_query(F.data == "referral")
async def cb_referral(c: CallbackQuery):
    uid = c.from_user.id
    refs = count_referrals(uid)
    text = (
        f"{LINE}\n   👥 РЕФЕРальная СИСТЕМА 👥\n{LINE}\n\n"
        "🔗 Твоя ссылка для друзей:\n\n"
        f"https://t.me/{BOT_USERNAME}?start=ref_{uid}\n\n"
        "💚 Что ты получаешь:\n"
        "🌿 Друг перешёл по ссылке → +1 день PREMIUM\n"
        "🌿 Друг выложил пост через бота → ещё +2 дня PREMIUM\n\n"
        f"📊 Ты уже пригласил: {refs} чел.\n"
        f"{LINE}\n"
        "🐸 Ква! Приглашай друзей и копи PREMIUM 💚"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Поделиться ссылкой",
                              url=f"https://t.me/share/url?url=https://t.me/{BOT_USERNAME}?start=ref_{uid}&text=🐸 Лови PostFrog — бота для постов и конкурсов!")],
        [InlineKeyboardButton(text="🐸 В меню", callback_data="menu")],
    ])
    await c.message.edit_text(text, reply_markup=kb)

# ======================== ПОДПИСКА (ЗВЁЗДЫ) ========================
@router.callback_query(F.data == "subscription")
async def cb_sub(c: CallbackQuery):
    u = get_user(c.from_user.id)
    await c.message.edit_text(
        f"{LINE}\n   ⭐ PREMIUM ПОДПИСКА ⭐\n{LINE}\n\n"
        f"Твой статус: {sub_text(u)}\n\n"
        "💚 Что даёт PREMIUM:\n"
        "🌿 Посты БЕЗ рекламы снизу\n"
        "🌿 Приоритетная поддержка\n\n"
        "👇 Выбирай тариф:",
        reply_markup=sub_buy_kb()
    )

SUB_PLANS = {"buy_7": (7, 9, "PREMIUM 7 дней"), "buy_31": (31, 29, "PREMIUM 31 день"),
             "buy_93": (93, 49, "PREMIUM 93 дня"), "buy_life": (0, 99, "PREMIUM навсегда")}

@router.callback_query(F.data.in_(SUB_PLANS.keys()))
async def cb_buy(c: CallbackQuery):
    days, stars, title = SUB_PLANS[c.data]
    await bot.send_invoice(
        chat_id=c.from_user.id,
        title=f"🐸 PostFrog — {title}",
        description=f"Подписка {title} для PostFrog",
        payload=f"sub_{c.data.split('_')[1]}",
        currency="XTR",
        prices=[LabeledPrice(label=title, amount=stars)]
    )

@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)

@router.message(F.successful_payment)
async def on_payment(message: Message):
    payload = message.successful_payment.invoice_payload
    key = payload.replace("sub_", "")
    if key == "life":
        days = 0
        add_sub_days(message.from_user.id, 0)
        await message.answer("🎉 Оплата прошла! Тебе выдана подписка PREMIUM НАВСЕГДА 💚🐸")
    else:
        days = int(key)
        add_sub_days(message.from_user.id, days)
        await message.answer(f"🎉 Оплата прошла! Подписка PREMIUM на {days} дн. активирована 💚🐸")
    stars = message.successful_payment.total_amount
    cur.execute("INSERT INTO purchases(user_id, stars, days, date) VALUES(?,?,?,?)",
                (message.from_user.id, stars, days, now_local().strftime(DB_FMT)))
    db.commit()

# ======================== ПРОМОКОД ========================
@router.callback_query(F.data == "promo")
async def cb_promo(c: CallbackQuery, state: FSMContext):
    await state.set_state(PromoIn.code)
    await c.message.edit_text(
        f"{LINE}\n   🎟 ПРОМОКОД 🎟\n{LINE}\n\n"
        "🔑 Введи промокод:\n\n"
        "Для отмены: /cancel"
    )

@router.message(PromoIn.code)
async def process_promo(message: Message, state: FSMContext):
    code = message.text.strip().upper()
    cur.execute("SELECT * FROM promos WHERE code=?", (code,))
    promo = cur.fetchone()
    if not promo:
        await message.answer("❌ Такого промокода нет. Попробуй ещё:")
        return
    if promo["used_by"]:
        await message.answer("⚠️ Этот промокод уже использован. Введи другой:")
        return
    cur.execute("UPDATE promos SET used_by=? WHERE code=?", (message.from_user.id, code))
    db.commit()
    add_sub_days(message.from_user.id, promo["days"])
    await state.clear()
    if promo["days"] == 0:
        await message.answer("🎉 Промокод активирован! PREMIUM НАВСЕГДА 💚🐸", reply_markup=back_menu())
    else:
        await message.answer(f"🎉 Промокод активирован! +{promo['days']} дн. PREMIUM 💚🐸",
                             reply_markup=back_menu())

# ======================== ДОБАВЛЕНИЕ ПОСТА ========================
@router.callback_query(F.data == "add_post")
async def cb_add_post(c: CallbackQuery, state: FSMContext):
    await state.clear()
    chans = user_channels(c.from_user.id)
    kb_rows = [[InlineKeyboardButton(text=f"📢 {ch['title']}",
                                     callback_data=f"pick_ch_{ch['chat_id']}")] for ch in chans]
    kb_rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="menu")])
    await state.set_state(AddPost.select_channel)
    await c.message.edit_text(
        f"{LINE}\n   📝 НОВЫЙ ПОСТ\n{LINE}\n\n"
        "👇 Выбери канал для публикации\n"
        "или перешли любое сообщение из канала:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows)
    )

@router.callback_query(AddPost.select_channel, F.data.startswith("pick_ch_"))
async def cb_pick_channel(c: CallbackQuery, state: FSMContext):
    chat_id = int(c.data.replace("pick_ch_", ""))
    if not await bot_is_admin(chat_id):
        await c.answer("⛔ Бот не админ в этом канале! Добавь меня и попробуй снова.",
                       show_alert=True)
        return
    cur.execute("SELECT title FROM channels WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    title = row["title"] if row else "Канал"
    await state.update_data(post={"chat_id": chat_id, "text": None, "photo": None,
                                  "btn_text": None, "btn_url": None,
                                  "delete_hours": None, "send_at": None})
    await state.set_state(AddPost.text)
    await c.message.edit_text(f"✅ Канал: {title}\n\n✍️ Введи текст поста:\n\nДля отмены: /cancel")

@router.message(AddPost.select_channel)
async def post_pick_forward(message: Message, state: FSMContext):
    chat = await extract_chat(message)
    if not chat:
        await message.answer("⚠️ Не понял канал. Перешли сообщение из канала или введи @username:")
        return
    chat_id, title = chat
    if not await bot_is_admin(chat_id):
        await message.answer("⛔ Добавьте в канал нашего бота для продолжения! 🐸")
        return
    save_channel(chat_id, title, message.from_user.id)
    await state.update_data(post={"chat_id": chat_id, "text": None, "photo": None,
                                  "btn_text": None, "btn_url": None,
                                  "delete_hours": None, "send_at": None})
    await state.set_state(AddPost.text)
    await message.answer(f"✅ Канал: {title}\n\n✍️ Теперь введи текст поста:")

@router.message(AddPost.text)
async def post_text(message: Message, state: FSMContext):
    data = await state.get_data()
    data["post"]["text"] = message.text
    await state.update_data(post=data["post"])
    await state.set_state(AddPost.photo)
    await message.answer(
        "🖼 Отправь фото для поста:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Пропустить", callback_data="post_no_photo")],
        ])
    )

@router.message(AddPost.photo, F.photo)
async def post_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    data["post"]["photo"] = message.photo[-1].file_id
    await state.update_data(post=data["post"])
    await ask_url_button(message, state)

@router.callback_query(AddPost.photo, F.data == "post_no_photo")
async def post_no_photo(c: CallbackQuery, state: FSMContext):
    await ask_url_button(c.message, state)

# ---------- URL-КНОПКА ----------
async def ask_url_button(message, state):
    await state.set_state(AddPost.btn_text)
    await message.answer(
        f"{LINE}\n   🔗 URL-КНОПКА\n{LINE}\n\n"
        "Хочешь добавить кнопку под постом?\n"
        "(например: ЖМИ 👇 → перебросит по ссылке)",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, добавить", callback_data="post_btn_yes"),
             InlineKeyboardButton(text="⏭ Нет, пропустить", callback_data="post_btn_no")],
        ])
    )

@router.callback_query(AddPost.btn_text, F.data == "post_btn_no")
async def post_btn_no(c: CallbackQuery, state: FSMContext):
    await ask_autodelete(c.message, state)

@router.callback_query(AddPost.btn_text, F.data == "post_btn_yes")
async def post_btn_yes(c: CallbackQuery, state: FSMContext):
    await c.message.edit_text("🔗 Введи НАЗВАНИЕ кнопки\n(например: Жми 👇):")

@router.message(AddPost.btn_text)
async def post_btn_name(message: Message, state: FSMContext):
    data = await state.get_data()
    data["post"]["btn_text"] = message.text[:64]
    await state.update_data(post=data["post"])
    await state.set_state(AddPost.btn_url)
    await message.answer("🌍 Теперь отправь ССЫЛКУ для кнопки\n(например: https://t.me/channel):")

@router.message(AddPost.btn_url)
async def post_btn_link(message: Message, state: FSMContext):
    u = message.text.strip()
    if not is_valid_url(u):
        await message.answer("⚠️ Ссылка должна начинаться с https://\nПопробуй ещё:")
        return
    data = await state.get_data()
    data["post"]["btn_url"] = u
    await state.update_data(post=data["post"])
    await ask_autodelete(message, state)

# ---------- АВТОУДАЛЕНИЕ ----------
async def ask_autodelete(message, state):
    await state.set_state(AddPost.autodelete)
    await message.answer(
        "🗑 Включить автоудаление поста?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да", callback_data="post_del_yes"),
             InlineKeyboardButton(text="❌ Нет", callback_data="post_del_no")],
        ])
    )

@router.callback_query(AddPost.autodelete, F.data == "post_del_yes")
async def post_del_yes(c: CallbackQuery, state: FSMContext):
    await state.set_state(AddPost.autodelete_hours)
    await c.message.edit_text("⏳ Через сколько часов удалить пост? Введи число:")

@router.message(AddPost.autodelete_hours)
async def post_del_hours(message: Message, state: FSMContext):
    try:
        hours = int(message.text.strip())
        if hours < 1:
            raise ValueError
    except:
        await message.answer("⚠️ Введи целое число ≥ 1:")
        return
    data = await state.get_data()
    data["post"]["delete_hours"] = hours
    await state.update_data(post=data["post"])
    await ask_when(message, state)

@router.callback_query(AddPost.autodelete, F.data == "post_del_no")
async def post_del_no(c: CallbackQuery, state: FSMContext):
    await ask_when(c.message, state)

# ---------- КОГДА ОТПРАВИТЬ ----------
async def ask_when(message, state):
    await state.set_state(AddPost.when)
    await message.answer(
        "📅 Когда отправить пост?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Сейчас", callback_data="post_now"),
             InlineKeyboardButton(text="⏰ По времени", callback_data="post_later")],
        ])
    )

@router.callback_query(AddPost.when, F.data == "post_now")
async def post_now(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    data["post"]["send_at"] = "now"
    await state.update_data(post=data["post"])
    await state.set_state(AddPost.confirm)
    await show_post_confirm(c.message, state, scheduled=False, user_id=c.from_user.id)

@router.callback_query(AddPost.when, F.data == "post_later")
async def post_later(c: CallbackQuery, state: FSMContext):
    await state.set_state(AddPost.schedule_time)
    await c.message.edit_text(
        f"⏰ Введи дату и время публикации\n\nФормат: {DATE_FMT}\nПример: 25.12.2025 18:00")

@router.message(AddPost.schedule_time)
async def post_time(message: Message, state: FSMContext):
    dt = parse_date(message.text)
    if not dt:
        await message.answer(f"⚠️ Неверный формат. Введи дату как: {DATE_FMT}")
        return
    if dt < now_local():
        await message.answer("⚠️ Эта дата уже прошла. Введи будущую:")
        return
    data = await state.get_data()
    data["post"]["send_at"] = dt.strftime(DB_FMT)
    await state.update_data(post=data["post"])
    await state.set_state(AddPost.confirm)
    await show_post_confirm(message, state, scheduled=True, user_id=message.from_user.id)

# ---------- ПОДТВЕРЖДЕНИЕ ----------
async def show_post_confirm(message, state, scheduled, user_id):
    data = await state.get_data()
    p = data["post"]
    if p["send_at"] == "now":
        when_txt = "📤 Сейчас"
    else:
        when_txt = "⏰ " + datetime.strptime(p["send_at"], DB_FMT).strftime(DATE_FMT)
    del_txt = f"через {p['delete_hours']} ч." if p["delete_hours"] else "нет"
    btn_txt = f"✅ [{p['btn_text']}]" if p["btn_text"] else "нет"
    u = get_user(user_id)
    prem = is_premium_user(u)
    ad_txt = "🌿 нет (PREMIUM)" if prem else "🌿 будет добавлена"
    preview = (p["text"] or "")[:300]
    text = (
        f"{LINE}\n   📋 ПРОВЕРЬ ПОСТ\n{LINE}\n\n"
        f"{preview}\n\n"
        "─────────────\n"
        f"🖼 Фото: {'есть ✅' if p['photo'] else 'нет ❌'}\n"
        f"🔗 Кнопка: {btn_txt}\n"
        f"📅 Отправка: {when_txt}\n"
        f"🗑 Автоудаление: {del_txt}\n"
        f"📢 Реклама снизу: {ad_txt}\n"
        f"{LINE}"
    )
    if scheduled:
        kb = [[InlineKeyboardButton(text="✅ Запланировать — выложится сам",
                                    callback_data="post_confirm")],
              [InlineKeyboardButton(text="📤 Выложить сейчас, не ждать", callback_data="post_publish_now")],
              [InlineKeyboardButton(text="✏️ Редактировать текст", callback_data="post_edit")],
              [InlineKeyboardButton(text="❌ Отменить", callback_data="post_cancel")]]
    else:
        kb = [[InlineKeyboardButton(text="✅ Выложить сейчас", callback_data="post_confirm")],
              [InlineKeyboardButton(text="✏️ Редактировать текст", callback_data="post_edit")],
              [InlineKeyboardButton(text="❌ Отмена", callback_data="post_cancel")]]
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(AddPost.confirm, F.data == "post_edit")
async def post_edit(c: CallbackQuery, state: FSMContext):
    await state.set_state(AddPost.edit_text)
    await c.message.edit_text("✏️ Введи новый текст поста:")

@router.message(AddPost.edit_text)
async def post_edit_text(message: Message, state: FSMContext):
    data = await state.get_data()
    data["post"]["text"] = message.text
    await state.update_data(post=data["post"])
    await state.set_state(AddPost.confirm)
    scheduled = data["post"]["send_at"] != "now"
    await show_post_confirm(message, state, scheduled, user_id=message.from_user.id)

@router.callback_query(AddPost.confirm, F.data == "post_cancel")
async def post_cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("❌ Пост отменён. Ква!", reply_markup=back_menu())

def save_post_to_db(owner_id, p, send_at_dt):
    cur.execute(
        "INSERT INTO posts(owner_id, chat_id, text, photo, btn_text, btn_url, delete_hours, send_at, status) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (owner_id, p["chat_id"], p["text"], p["photo"], p["btn_text"], p["btn_url"],
         p["delete_hours"], send_at_dt.strftime(DB_FMT), "scheduled")
    )
    db.commit()
    return cur.lastrowid

def reward_referrer_for_first_post(owner_id):
    cur.execute("SELECT COUNT(*) c FROM posts WHERE owner_id=?", (owner_id,))
    if cur.fetchone()["c"] > 1:
        return None
    u = get_user(owner_id)
    if u and u["referred_by"]:
        add_sub_days(u["referred_by"], 2)
        return u["referred_by"]
    return None

async def notify_referrer_bonus(ref_id):
    if not ref_id:
        return
    try:
        await bot.send_message(
            ref_id,
            f"{LINE}\n   🎉 БОНУС РЕФЕРАЛА! 🎉\n{LINE}\n\n"
            "🐸 Твой друг выложил первый пост через бота!\n"
            "💚 Тебе начислен +2 дня PREMIUM!"
        )
    except:
        pass

@router.callback_query(AddPost.confirm, F.data == "post_confirm")
async def post_confirm(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    p = data["post"]
    await state.clear()

    if p["send_at"] == "now":
        post_id = save_post_to_db(c.from_user.id, p, now_local())
        ref = reward_referrer_for_first_post(c.from_user.id)
        ok = await publish_post_by_id(post_id)
        if ok:
            await c.message.edit_text("✅ Пост опубликован в канале! 🐸💚", reply_markup=back_menu())
        else:
            await c.message.edit_text(
                "⚠️ Пост НЕ удалось опубликовать!\n"
                "🐸 Проверь личные сообщения — там подробности.",
                reply_markup=back_menu()
            )
        await notify_referrer_bonus(ref)
    else:
        send_dt = datetime.strptime(p["send_at"], DB_FMT)
        post_id = save_post_to_db(c.from_user.id, p, send_dt)
        start_post_timer(post_id, send_dt)  # ⏰ точный таймер на публикацию
        ref = reward_referrer_for_first_post(c.from_user.id)
        await notify_referrer_bonus(ref)
        await c.message.edit_text(
            f"✅ Пост #{post_id} запланирован на {send_dt.strftime(DATE_FMT)}\n\n"
            "🐸 Он выложится АВТОМАТИЧЕСКИ в указанное время —\n"
            "ничего нажимать не нужно!\n\n"
            "🩺 Проверить: /debug",
            reply_markup=back_menu()
        )
        log.info(f"[⏰] Пост #{post_id} запланирован на {p['send_at']} (таймер запущен)")

@router.callback_query(AddPost.confirm, F.data == "post_publish_now")
async def post_publish_now(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    p = data["post"]
    await state.clear()
    post_id = save_post_to_db(c.from_user.id, p, now_local())
    ref = reward_referrer_for_first_post(c.from_user.id)
    ok = await publish_post_by_id(post_id)
    if ok:
        await c.message.edit_text("✅ Пост опубликован СРАЗУ (без ожидания)! 🐸💚", reply_markup=back_menu())
    else:
        await c.message.edit_text(
            "⚠️ Пост НЕ удалось опубликовать!\n"
            "🐸 Проверь личные сообщения — там подробности.",
            reply_markup=back_menu()
        )
    await notify_referrer_bonus(ref)

# ======================== КОНКУРСЫ ========================
@router.callback_query(F.data == "new_giveaway")
async def cb_new_gw(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.update_data(gw={"channels": []})
    await state.set_state(Giveaway.description)
    await c.message.edit_text(
        f"{LINE}\n   🎁 СОЗДАНИЕ КОНКУРСА\n{LINE}\n\n"
        "Шаг 1/7 — введи описание конкурса\n"
        "(что разыгрывается, условия и т.д.):\n\n"
        "Для отмены: /cancel"
    )

@router.message(Giveaway.description)
async def gw_description(message: Message, state: FSMContext):
    data = await state.get_data()
    data["gw"]["description"] = message.text
    await state.update_data(gw=data["gw"])
    await state.set_state(Giveaway.photo)
    await message.answer(
        "🌿 Шаг 2/7 — отправь фото для конкурса:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Пропустить", callback_data="gw_no_photo")],
        ])
    )

@router.message(Giveaway.photo, F.photo)
async def gw_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    data["gw"]["photo"] = message.photo[-1].file_id
    await state.update_data(gw=data["gw"])
    await gw_ask_winners(message, state)

@router.callback_query(Giveaway.photo, F.data == "gw_no_photo")
async def gw_no_photo(c: CallbackQuery, state: FSMContext):
    await gw_ask_winners(c.message, state)

async def gw_ask_winners(message, state):
    await state.set_state(Giveaway.winners)
    await message.answer("🏆 Шаг 3/7 — введи число победителей (от 1 до 100000):")

@router.message(Giveaway.winners)
async def gw_winners(message: Message, state: FSMContext):
    try:
        n = int(message.text.strip())
        if not 1 <= n <= 100000:
            raise ValueError
    except:
        await message.answer("⚠️ Введи число от 1 до 100000:")
        return
    data = await state.get_data()
    data["gw"]["winners"] = n
    await state.update_data(gw=data["gw"])
    await state.set_state(Giveaway.condition)
    await message.answer(
        "🌿 Шаг 4/7 — выбери условия конкурса:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Обязательная подписка", callback_data="gw_sub_yes")],
            [InlineKeyboardButton(text="⏭ Пропустить", callback_data="gw_sub_no")],
        ])
    )

@router.callback_query(Giveaway.condition, F.data == "gw_sub_no")
async def gw_sub_no(c: CallbackQuery, state: FSMContext):
    await gw_select_channel(c.message, state, c.from_user.id)

@router.callback_query(Giveaway.condition, F.data == "gw_sub_yes")
async def gw_sub_yes(c: CallbackQuery, state: FSMContext):
    await state.set_state(Giveaway.add_channel)
    await c.message.edit_text(
        "📣 Перешли сообщение из канала для обязательной подписки\nили введи @username канала:"
    )

@router.message(Giveaway.add_channel)
async def gw_add_channel(message: Message, state: FSMContext):
    chat = await extract_chat(message)
    if not chat:
        await message.answer("⚠️ Не понял канал. Перешли сообщение из канала или введи @username:")
        return
    chat_id, title = chat
    if not await bot_is_admin(chat_id):
        await message.answer("⛔ Добавьте в канал нашего бота для продолжения! 🐸")
        return
    data = await state.get_data()
    chans = data["gw"]["channels"]
    if any(ch[0] == chat_id for ch in chans):
        await message.answer("⚠️ Этот канал уже добавлен.")
        return
    chans.append((chat_id, title))
    data["gw"]["channels"] = chans
    await state.update_data(gw=data["gw"])
    lst = "\n".join(f"  🌿 {t}" for _, t in chans)
    await message.answer(
        f"✅ Канал {title} добавлен!\n\n"
        f"📣 Каналы для подписки:\n{lst}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить ещё", callback_data="gw_add_more")],
            [InlineKeyboardButton(text="➡️ Далее", callback_data="gw_channels_done")],
        ])
    )

@router.callback_query(Giveaway.add_channel, F.data == "gw_add_more")
async def gw_add_more(c: CallbackQuery, state: FSMContext):
    await state.set_state(Giveaway.add_channel)
    await c.message.edit_text("📣 Перешли сообщение из следующего канала или введи @username:")

@router.callback_query(Giveaway.add_channel, F.data == "gw_channels_done")
async def gw_channels_done(c: CallbackQuery, state: FSMContext):
    await gw_select_channel(c.message, state, c.from_user.id)

async def gw_select_channel(message, state, user_id):
    chans = user_channels(user_id)
    kb_rows = [[InlineKeyboardButton(text=f"📢 {ch['title']}",
                                     callback_data=f"gw_ch_{ch['chat_id']}")] for ch in chans]
    kb_rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="menu")])
    await state.set_state(Giveaway.select_channel)
    await message.answer(
        "📢 Шаг 4.5 — выбери канал, куда выложить конкурс\nили перешли сообщение из канала:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows)
    )

@router.callback_query(Giveaway.select_channel, F.data.startswith("gw_ch_"))
async def gw_ch_pick(c: CallbackQuery, state: FSMContext):
    chat_id = int(c.data.replace("gw_ch_", ""))
    if not await bot_is_admin(chat_id):
        await c.answer("⛔ Бот не админ в этом канале!", show_alert=True)
        return
    cur.execute("SELECT title FROM channels WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    title = row["title"] if row else "Канал"
    data = await state.get_data()
    data["gw"]["channel_id"] = chat_id
    data["gw"]["channel_title"] = title
    data["gw"]["owner_id"] = c.from_user.id
    await state.update_data(gw=data["gw"])
    await state.set_state(Giveaway.end_type)
    await c.message.edit_text(
        "🌿 Шаг 5/7 — когда подвести итоги конкурса?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏰ По времени", callback_data="gw_end_time"),
             InlineKeyboardButton(text="👥 По числу участников", callback_data="gw_end_count")],
        ])
    )

@router.message(Giveaway.select_channel)
async def gw_pick_forward(message: Message, state: FSMContext):
    chat = await extract_chat(message)
    if not chat:
        await message.answer("⚠️ Не понял канал. Перешли сообщение из канала или введи @username:")
        return
    chat_id, title = chat
    if not await bot_is_admin(chat_id):
        await message.answer("⛔ Добавьте в канал нашего бота для продолжения! 🐸")
        return
    save_channel(chat_id, title, message.from_user.id)
    data = await state.get_data()
    data["gw"]["channel_id"] = chat_id
    data["gw"]["channel_title"] = title
    data["gw"]["owner_id"] = message.from_user.id
    await state.update_data(gw=data["gw"])
    await state.set_state(Giveaway.end_type)
    await message.answer(
        "🌿 Шаг 5/7 — когда подвести итоги конкурса?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏰ По времени", callback_data="gw_end_time"),
             InlineKeyboardButton(text="👥 По числу участников", callback_data="gw_end_count")],
        ])
    )

@router.callback_query(Giveaway.end_type, F.data == "gw_end_time")
async def gw_end_time(c: CallbackQuery, state: FSMContext):
    await state.set_state(Giveaway.end_time)
    await c.message.edit_text(f"⏰ Введи дату и время подведения итогов:\n\nФормат: {DATE_FMT}")

@router.message(Giveaway.end_time)
async def gw_end_time_input(message: Message, state: FSMContext):
    dt = parse_date(message.text)
    if not dt or dt < now_local():
        await message.answer(f"⚠️ Введи корректную будущую дату ({DATE_FMT}):")
        return
    data = await state.get_data()
    data["gw"]["end_type"] = "time"
    data["gw"]["end_value"] = dt.strftime(DB_FMT)
    await state.update_data(gw=data["gw"])
    await gw_ask_publish(message, state)

@router.callback_query(Giveaway.end_type, F.data == "gw_end_count")
async def gw_end_count(c: CallbackQuery, state: FSMContext):
    await state.set_state(Giveaway.end_count)
    await c.message.edit_text("👥 Введи число участников для подведения итогов:")

@router.message(Giveaway.end_count)
async def gw_end_count_input(message: Message, state: FSMContext):
    try:
        n = int(message.text.strip())
        if n < 1:
            raise ValueError
    except:
        await message.answer("⚠️ Введи целое число ≥ 1:")
        return
    data = await state.get_data()
    data["gw"]["end_type"] = "count"
    data["gw"]["end_value"] = str(n)
    await state.update_data(gw=data["gw"])
    await gw_ask_publish(message, state)

async def gw_ask_publish(message, state):
    await state.set_state(Giveaway.publish_when)
    await message.answer(
        "🌿 Шаг 6/7 — когда опубликовать конкурс?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Сейчас", callback_data="gw_pub_now"),
             InlineKeyboardButton(text="⏰ Выбрать время", callback_data="gw_pub_later")],
        ])
    )

@router.callback_query(Giveaway.publish_when, F.data == "gw_pub_later")
async def gw_pub_later(c: CallbackQuery, state: FSMContext):
    await state.set_state(Giveaway.publish_time)
    await c.message.edit_text(f"⏰ Введи дату и время публикации:\n\nФормат: {DATE_FMT}")

@router.message(Giveaway.publish_time)
async def gw_pub_time(message: Message, state: FSMContext):
    dt = parse_date(message.text)
    if not dt or dt < now_local():
        await message.answer(f"⚠️ Введи корректную будущую дату ({DATE_FMT}):")
        return
    await save_and_schedule_giveaway(message, state, dt)

@router.callback_query(Giveaway.publish_when, F.data == "gw_pub_now")
async def gw_pub_now(c: CallbackQuery, state: FSMContext):
    await save_and_schedule_giveaway(c.message, state, now_local())

async def save_and_schedule_giveaway(message, state, dt):
    data = await state.get_data()
    gw = data["gw"]

    if gw["channels"]:
        fc_id, fc_title = get_force_channel()
        if fc_id and not any(ch[0] == fc_id for ch in gw["channels"]):
            gw["channels"].append((fc_id, fc_title))

    cur.execute(
        "INSERT INTO giveaways(owner_id, channel_id, description, photo, winners, sub_required, "
        "end_type, end_value, publish_at, status) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (gw["owner_id"], gw["channel_id"], gw["description"], gw.get("photo"),
         gw["winners"], 1 if gw["channels"] else 0,
         gw["end_type"], gw["end_value"], dt.strftime(DB_FMT), "created")
    )
    db.commit()
    gid = cur.lastrowid
    for chat_id, title in gw["channels"]:
        cur.execute("INSERT INTO g_channels(giveaway_id, chat_id, title) VALUES(?,?,?)",
                    (gid, chat_id, title))
    db.commit()
    await state.clear()

    # ⏰ точные таймеры на публикацию и итоги
    if dt > now_local():
        start_giveaway_publish_timer(gid, dt)
    if gw["end_type"] == "time":
        end_dt = datetime.strptime(gw["end_value"], DB_FMT)
        if end_dt > now_local():
            start_giveaway_finish_timer(gid, end_dt)

    if dt <= now_local():
        await publish_giveaway(gid)
        await message.answer("🎉 Конкурс опубликован в канале! 🐸💚", reply_markup=back_menu())
    else:
        await message.answer(
            f"✅ Конкурс #{gid} запланирован на {dt.strftime(DATE_FMT)}\n\n"
            "🐸 Выложится АВТОМАТИЧЕСКИ, итоги подведутся сами!\n"
            "🩺 Проверить: /debug",
            reply_markup=back_menu()
        )
        log.info(f"[⏰] Конкурс #{gid} запланирован (таймеры запущены)")

async def publish_giveaway(gid):
    cur.execute("SELECT * FROM giveaways WHERE id=?", (gid,))
    g = cur.fetchone()
    if not g or g["status"] != "created":
        return
    cur.execute("UPDATE giveaways SET status='publishing' WHERE id=? AND status='created'", (gid,))
    db.commit()
    if cur.rowcount == 0:
        return
    cur.execute("SELECT * FROM g_channels WHERE giveaway_id=?", (gid,))
    req = cur.fetchall()
    cond = ""
    if req:
        titles = "\n".join(f"  🌿 {r['title']}" for r in req)
        cond = f"\n📌 Условие — подписка на каналы:\n{titles}\n"
    text = (
        f"{LINE}\n"
        "   🎉 КОНКУРС 🎉\n"
        f"{LINE}\n\n"
        f"{g['description']}\n\n"
        f"🏆 Победителей: {g['winners']}"
        f"{cond}"
        f"\n⏳ Итоги: {'по времени — ' + g['end_value'] if g['end_type']=='time' else 'после ' + g['end_value'] + ' участников'}\n"
        f"{LINE}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🐸 Участвовать!",
                              url=f"https://t.me/{BOT_USERNAME}?start=gw_{gid}")]
    ])
    msg = None
    try:
        if g["photo"]:
            msg = await bot.send_photo(g["channel_id"], g["photo"], caption=text[:1024], reply_markup=kb)
        else:
            msg = await bot.send_message(g["channel_id"], text[:4096], reply_markup=kb)
    except Exception as e:
        log.error(f"Ошибка публикации конкурса {gid}: {e}")
        try:
            await bot.send_message(g["owner_id"],
                                   f"⚠️ Не удалось опубликовать конкурс #{gid}.\n"
                                   "🐸 Проверь, что бот — админ канала!")
        except:
            pass
    cur.execute("UPDATE giveaways SET status='running', post_message_id=? WHERE id=?",
                (msg.message_id if msg else None, gid))
    db.commit()
    log.info(f"[✓] Конкурс #{gid} опубликован")

# ---------- Участие ----------
async def try_join_giveaway(user_id, gid):
    cur.execute("SELECT * FROM giveaways WHERE id=?", (gid,))
    g = cur.fetchone()
    if not g:
        await bot.send_message(user_id, "❌ Конкурс не найден.")
        return
    if g["status"] != "running":
        await bot.send_message(user_id, "⏳ Этот конкурс уже завершён или ещё не начался.")
        return
    cur.execute("SELECT * FROM participants WHERE giveaway_id=? AND user_id=?", (gid, user_id))
    if cur.fetchone():
        await bot.send_message(user_id, "😎 Ты уже участвуешь в этом конкурсе! 🐸")
        await send_gw_stats(user_id, gid)
        return
    cur.execute("SELECT * FROM g_channels WHERE giveaway_id=?", (gid,))
    req = cur.fetchall()
    not_subscribed = []
    for r in req:
        if not await user_subscribed(r["chat_id"], user_id):
            not_subscribed.append(r)
    if not_subscribed:
        kb_rows = []
        for r in not_subscribed:
            try:
                chat = await bot.get_chat(r["chat_id"])
                if chat.username:
                    kb_rows.append([InlineKeyboardButton(text=f"📢 {r['title']}",
                                                         url=f"https://t.me/{chat.username}")])
            except:
                pass
        kb_rows.append([InlineKeyboardButton(text="✅ Я подписался", callback_data=f"gw_check_{gid}")])
        await bot.send_message(
            user_id,
            f"{LINE}\n   🔔 ПОДПИШИСЬ 🔔\n{LINE}\n\n"
            "🐸 Для участия сначала подпишись на каналы:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows)
        )
        return
    cur.execute("INSERT INTO participants(giveaway_id, user_id) VALUES(?,?)", (gid, user_id))
    db.commit()
    await bot.send_message(user_id, "🎉 Ты участвуешь в конкурсе! Ква-ква! 🐸💚")
    await send_gw_stats(user_id, gid)
    cur.execute("SELECT COUNT(*) c FROM participants WHERE giveaway_id=?", (gid,))
    cnt = cur.fetchone()["c"]
    if g["end_type"] == "count" and cnt >= int(g["end_value"]):
        await finish_giveaway(gid)

@router.callback_query(F.data.startswith("gw_check_"))
async def cb_gw_check(c: CallbackQuery):
    gid = int(c.data.replace("gw_check_", ""))
    await c.message.delete()
    await try_join_giveaway(c.from_user.id, gid)

async def send_gw_stats(user_id, gid):
    cur.execute("SELECT * FROM giveaways WHERE id=?", (gid,))
    g = cur.fetchone()
    cur.execute("SELECT COUNT(*) c FROM participants WHERE giveaway_id=?", (gid,))
    cnt = cur.fetchone()["c"]
    if g["end_type"] == "time":
        end = datetime.strptime(g["end_value"], DB_FMT)
        delta = end - now_local()
        h, m = delta.seconds // 3600, (delta.seconds // 60) % 60
        left = f"⏳ До конца: {delta.days} дн. {h} ч. {m} мин."
    else:
        left = f"⏳ До конца: ещё {max(0, int(g['end_value']) - cnt)} участников"
    await bot.send_message(
        user_id,
        f"{LINE}\n   📊 СТАТИСТИКА КОНКУРСА\n{LINE}\n\n"
        f"🎁 Конкурс #{gid}\n"
        f"👥 Участников: {cnt}\n"
        f"{left}\n"
        f"{LINE}"
    )

# ---------- Мои конкурсы ----------
@router.callback_query(F.data == "my_giveaways")
async def cb_my_gw(c: CallbackQuery):
    cur.execute("SELECT * FROM giveaways WHERE owner_id=? ORDER BY id DESC LIMIT 10", (c.from_user.id,))
    gws = cur.fetchall()
    if not gws:
        await c.message.edit_text("📭 У тебя пока нет конкурсов. Ква 🐸", reply_markup=back_menu())
        return
    status_icon = {"created": "🕒", "running": "🟢", "finished": "🏁"}
    text = f"{LINE}\n   🏆 МОИ КОНКУРСЫ 🏆\n{LINE}\n\n"
    for g in gws:
        cur.execute("SELECT COUNT(*) c FROM participants WHERE giveaway_id=?", (g["id"],))
        cnt = cur.fetchone()["c"]
        text += f"{status_icon.get(g['status'], '•')} #{g['id']} — 👥 {cnt} — {g['status']}\n"
    text += LINE
    await c.message.edit_text(text, reply_markup=back_menu())

# ---------- Завершение конкурса (с защитой от дублей) ----------
async def finish_giveaway(gid):
    cur.execute("SELECT * FROM giveaways WHERE id=?", (gid,))
    g = cur.fetchone()
    if not g or g["status"] != "running":
        return
    cur.execute("UPDATE giveaways SET status='finishing' WHERE id=? AND status='running'", (gid,))
    db.commit()
    if cur.rowcount == 0:
        return  # уже завершает другой процесс
    log.info(f"[🏁] Подвожу итоги конкурса #{gid}...")
    cur.execute("SELECT user_id FROM participants WHERE giveaway_id=?", (gid,))
    participants = [r["user_id"] for r in cur.fetchall()]
    if not participants:
        text = f"{LINE}\n   🏁 ИТОГИ КОНКУРСА 🏁\n{LINE}\n\n😔 Участников не было. Ква..."
        try:
            await bot.send_message(g["channel_id"], text)
        except:
            pass
        cur.execute("UPDATE giveaways SET status='finished' WHERE id=?", (gid,))
        db.commit()
        log.info(f"[🏁] Конкурс #{gid} завершён (без участников)")
        return
    winners_count = min(g["winners"], len(participants))
    winners = random.sample(participants, winners_count)
    mentions = []
    for w in winners:
        try:
            chat = await bot.get_chat(w)
            name = chat.first_name or chat.title or "Участник"
        except:
            name = "Участник"
        mentions.append(f'🏆 <a href="tg://user?id={w}">{html.escape(name)}</a>')
    text = (
        f"{LINE}\n"
        "   🏁 ИТОГИ КОНКУРСА 🏁\n"
        f"{LINE}\n\n"
        f"🎉 Победители ({len(winners)}):\n\n" + "\n".join(mentions) +
        "\n\n🐸💚 Поздравляем победителей!"
    )
    sent = False
    try:
        await bot.send_message(g["channel_id"], text, parse_mode="HTML")
        sent = True
        log.info(f"[🏁] Итоги конкурса #{gid} отправлены в {g['channel_id']}")
    except Exception as e:
        log.error(f"Ошибка итогов конкурса {gid}: {e}")
        try:
            plain = (f"🏁 ИТОГИ КОНКУРСА 🏁\n\n🎉 Победители ({len(winners)}): " +
                     ", ".join(f"ID {w}" for w in winners))
            await bot.send_message(g["channel_id"], plain)
            sent = True
        except:
            pass
    if g["post_message_id"]:
        try:
            await bot.edit_message_reply_markup(g["channel_id"], g["post_message_id"], reply_markup=None)
        except:
            pass
    if sent:
        try:
            await bot.send_message(g["owner_id"], f"🏁 Итоги конкурса #{gid} подведены и отправлены в канал! 🐸")
        except:
            pass
    cur.execute("UPDATE giveaways SET status='finished' WHERE id=?", (gid,))
    db.commit()

# ======================== 📊 ОТЧЁТ ПО КАНАЛУ ========================
@router.callback_query(F.data == "report")
async def cb_report(c: CallbackQuery, state: FSMContext):
    await state.clear()
    chans = user_channels(c.from_user.id)
    kb_rows = [[InlineKeyboardButton(text=f"📊 {ch['title']}",
                                     callback_data=f"rep_ch_{ch['chat_id']}")] for ch in chans]
    kb_rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="menu")])
    await state.set_state(ReportFSM.channel)
    await c.message.edit_text(
        f"{LINE}\n   📊 ОТЧЁТ ПО КАНАЛУ 📊\n{LINE}\n\n"
        "👇 Выбери канал из списка\n"
        "или перешли сообщение из канала / введи @username:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows)
    )

@router.callback_query(ReportFSM.channel, F.data.startswith("rep_ch_"))
async def rep_ch_pick(c: CallbackQuery, state: FSMContext):
    chat_id = int(c.data.replace("rep_ch_", ""))
    cur.execute("SELECT title FROM channels WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    title = row["title"] if row else "Канал"
    await state.clear()
    report = await build_channel_report(chat_id, title)
    await c.message.edit_text(report, reply_markup=back_menu())

@router.message(ReportFSM.channel)
async def rep_forward(message: Message, state: FSMContext):
    chat = await extract_chat(message)
    if not chat:
        await message.answer("⚠️ Не понял канал. Перешли сообщение из канала или введи @username:")
        return
    chat_id, title = chat
    await state.clear()
    report = await build_channel_report(chat_id, title)
    await message.answer(report, reply_markup=back_menu())

async def build_channel_report(chat_id, title):
    try:
        subs = await bot.get_chat_member_count(chat_id)
    except:
        subs = 0
    cur.execute("SELECT COALESCE(SUM(posts),0) p, COALESCE(SUM(reactions),0) r "
                "FROM chan_stats WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    posts, reactions = row["p"], row["r"]
    views = int(subs * 0.3)
    cost = subs * 0.05 + (views / 10) * 4
    return (
        f"{LINE}\n"
        "   📊 ОТЧЁТ ПО КАНАЛУ 📊\n"
        f"{LINE}\n\n"
        f"📢 {title}\n\n"
        f"👥 Подписчиков: {subs}\n"
        f"📝 Постов: {posts}\n"
        f"💚 Реакций: {reactions}\n"
        f"👀 Охват (~30%): {views}\n"
        "─────────────\n"
        f"💰 Цена рекламы 24ч: {round(cost, 2)} ₽\n"
        "─────────────\n"
        "🧮 Формула подсчёта:\n"
        "🌿 1 подписчик = +0.05 ₽\n"
        "🌿 10 просмотров = +4 ₽\n"
        f"{LINE}\n"
        "🐸 Ква! Отчёт готов 💚"
    )

# ======================== /admin ========================
def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎟 Сделать промокод", callback_data="adm_promo")],
        [InlineKeyboardButton(text="⭐ Выдать подписку", callback_data="adm_give"),
         InlineKeyboardButton(text="🚫 Забрать подписку", callback_data="adm_take")],
        [InlineKeyboardButton(text="📢 Текст к постам", callback_data="adm_ad")],
        [InlineKeyboardButton(text="🐸 Мой обяз. канал", callback_data="adm_force")],
        [InlineKeyboardButton(text="📈 Статистика", callback_data="adm_stats")],
        [InlineKeyboardButton(text="📨 Рассылка всем", callback_data="adm_bc")],
    ])

def admin_text():
    ad = get_ad_text()
    fc_id, fc_title = get_force_channel()
    return (
        f"{LINE}\n"
        "   👑 АДМИН-ПАНЕЛЬ POSTFROG 👑\n"
        f"{LINE}\n\n"
        f"📢 Текст к постам: {'✅ установлен' if ad else '❌ не установлен'}\n"
        f"🐸 Обяз. канал: {'✅ ' + fc_title if fc_title else '❌ не задан'}\n"
        f"{LINE}"
    )

@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Ква! Доступ запрещён. 🐸")
        return
    await state.clear()
    await message.answer(admin_text(), reply_markup=admin_kb())

@router.callback_query(F.data == "adm_back")
async def cb_adm_back(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await c.message.edit_text(admin_text(), reply_markup=admin_kb())

@router.callback_query(F.data.startswith("adm_"))
async def cb_admin(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID:
        await c.answer("⛔ Нет доступа", show_alert=True)
        return
    act = c.data.replace("adm_", "")
    if act == "promo":
        await state.set_state(AdminFSM.promo_code)
        await c.message.edit_text("🎟 Введи код промокода (латиницей/цифрами):")
    elif act == "give":
        await state.set_state(AdminFSM.give_id)
        await c.message.edit_text("⭐ Введи ID пользователя для выдачи подписки:")
    elif act == "take":
        await state.set_state(AdminFSM.take_id)
        await c.message.edit_text("🚫 Введи ID пользователя для изъятия подписки:")
    elif act == "ad":
        ad = get_ad_text()
        await c.message.edit_text(
            f"📢 Текущий текст к постам:\n\n{ad or '— не установлен —'}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✏️ Изменить/Добавить", callback_data="adm_ad_edit")],
                [InlineKeyboardButton(text="🗑 Удалить", callback_data="adm_ad_del")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back")],
            ])
        )
    elif act == "ad_edit":
        await state.set_state(AdminFSM.ad_text)
        await c.message.edit_text("📢 Введи текст (реклама), который добавится снизу всех постов:\n\n/cancel — отмена")
    elif act == "ad_del":
        del_setting("ad_text")
        await c.message.edit_text("🗑 Текст удалён.", reply_markup=admin_kb())
    elif act == "force":
        fc_id, fc_title = get_force_channel()
        kb_rows = [[InlineKeyboardButton(text="🗑 Удалить обяз. канал", callback_data="adm_force_del")],
                   [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back")]]
        await c.message.edit_text(
            f"🐸 Твой обязательный канал:\n\n"
            f"{'✅ ' + fc_title if fc_title else '❌ не задан'}\n\n"
            "Канал будет автоматически добавляться к конкурсам с обязательной подпиской.\n\n"
            "👇 Отправь @username канала или перешли сообщение из него:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows)
        )
        await state.set_state(AdminFSM.force_channel)
    elif act == "force_del":
        del_setting("force_channel_id")
        del_setting("force_channel_title")
        await c.message.edit_text("🗑 Обязательный канал удалён.", reply_markup=admin_kb())
        await state.clear()
    elif act == "stats":
        cur.execute("SELECT COUNT(*) c FROM users")
        users = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) c FROM users WHERE sub_type!='free'")
        prem = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) c FROM channels")
        chans = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) c FROM posts WHERE status IN ('published','deleted')")
        posts_pub = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) c FROM giveaways")
        gws = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) c FROM users WHERE referred_by IS NOT NULL")
        refs = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(DISTINCT user_id) c FROM purchases")
        buyers = cur.fetchone()["c"]
        total_days = 0
        lifetime_count = 0
        cur.execute("SELECT sub_type, sub_until FROM users")
        now = now_local()
        for r in cur.fetchall():
            if r["sub_type"] == "lifetime":
                lifetime_count += 1
            elif r["sub_type"] == "premium" and r["sub_until"]:
                try:
                    until = datetime.strptime(r["sub_until"], DB_FMT)
                    if until > now:
                        total_days += (until - now).days
                except:
                    pass
        await c.message.edit_text(
            f"{LINE}\n   📈 СТАТИСТИКА POSTFROG 📈\n{LINE}\n\n"
            f"👥 Пользователей: {users}\n"
            f"⭐ С подпиской: {prem}\n"
            f"📡 Каналов: {chans}\n"
            "─────────────\n"
            f"📝 Постов выложено: {posts_pub}\n"
            f"🎁 Конкурсов создано: {gws}\n"
            f"👥 Рефералов всего: {refs}\n"
            f"💳 Купили подписку: {buyers} чел.\n"
            f"⏳ Дней подписок у всех: {total_days}"
            + (f" (+{lifetime_count} навсегда ♾)" if lifetime_count else "") + "\n"
            f"{LINE}"
        )
    elif act == "bc":
        await state.set_state(AdminFSM.broadcast)
        await c.message.edit_text("📨 Отправь сообщение для рассылки всем пользователям\n(текст, фото, видео):")

# ---------- Админ FSM ----------
@router.message(AdminFSM.force_channel)
async def adm_force_channel(message: Message, state: FSMContext):
    chat = await extract_chat(message)
    if not chat:
        await message.answer("⚠️ Не понял канал. Перешли сообщение или введи @username:")
        return
    chat_id, title = chat
    if not await bot_is_admin(chat_id):
        await message.answer("⛔ Сначала добавьте бота админом в этот канал! 🐸")
        return
    set_setting("force_channel_id", chat_id)
    set_setting("force_channel_title", title)
    await state.clear()
    await message.answer(f"✅ Обязательный канал: {title}\nТеперь он будет добавляться ко всем конкурсам с подпиской! 🐸💚")

@router.message(AdminFSM.promo_code)
async def adm_promo_code(message: Message, state: FSMContext):
    await state.update_data(code=message.text.strip().upper())
    await state.set_state(AdminFSM.promo_days)
    await message.answer("⏳ На сколько дней подписка? (0 = навсегда):")

@router.message(AdminFSM.promo_days)
async def adm_promo_days(message: Message, state: FSMContext):
    try:
        days = int(message.text.strip())
        if days < 0:
            raise ValueError
    except:
        await message.answer("⚠️ Введи число ≥ 0:")
        return
    data = await state.get_data()
    try:
        cur.execute("INSERT INTO promos(code, days) VALUES(?,?)", (data["code"], days))
        db.commit()
        d = "НАВСЕГДА" if days == 0 else f"{days} дн."
        await message.answer(f"✅ Промокод {data['code']} создан ({d}) 🐸")
    except:
        await message.answer("⚠️ Такой промокод уже существует.")
    await state.clear()

@router.message(AdminFSM.give_id)
async def adm_give_id(message: Message, state: FSMContext):
    try:
        uid = int(message.text.strip())
    except:
        await message.answer("⚠️ Введи числовой ID:")
        return
    await state.update_data(uid=uid)
    await state.set_state(AdminFSM.give_days)
    await message.answer("⏳ Сколько дней подписки выдать? (0 = навсегда):")

@router.message(AdminFSM.give_days)
async def adm_give_days(message: Message, state: FSMContext):
    try:
        days = int(message.text.strip())
        if days < 0:
            raise ValueError
    except:
        await message.answer("⚠️ Введи число ≥ 0:")
        return
    data = await state.get_data()
    add_sub_days(data["uid"], days)
    await state.clear()
    await message.answer(f"✅ Пользователю {data['uid']} выдана подписка "
                         f"({'навсегда' if days == 0 else str(days) + ' дн.'}) 🐸")
    try:
        await bot.send_message(data["uid"], "🎉 Тебе выдана подписка PREMIUM администратором! 🐸💚")
    except:
        pass

@router.message(AdminFSM.take_id)
async def adm_take_id(message: Message, state: FSMContext):
    try:
        uid = int(message.text.strip())
    except:
        await message.answer("⚠️ Введи числовой ID:")
        return
    remove_sub(uid)
    await state.clear()
    await message.answer(f"✅ Подписка у {uid} забрана. Ква 🐸")

@router.message(AdminFSM.ad_text)
async def adm_ad_text(message: Message, state: FSMContext):
    set_setting("ad_text", message.text)
    await state.clear()
    await message.answer("✅ Текст сохранён! Теперь он добавляется снизу всех постов "
                         "(кроме постов PREMIUM-пользователей) 🐸")

@router.message(AdminFSM.broadcast)
async def adm_broadcast(message: Message, state: FSMContext):
    await state.update_data(bc_chat=message.chat.id, bc_msg=message.message_id)
    await state.set_state(AdminFSM.broadcast_confirm)
    await message.answer(
        "📨 Отправить это сообщение всем пользователям?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, разослать", callback_data="bc_go")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="adm_back")],
        ])
    )

@router.callback_query(AdminFSM.broadcast_confirm, F.data == "bc_go")
async def bc_go(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    await c.message.edit_text("📨 Рассылка запущена... 🐸")
    cur.execute("SELECT user_id FROM users")
    users = cur.fetchall()
    ok, fail = 0, 0
    for u in users:
        try:
            await bot.copy_message(u["user_id"], data["bc_chat"], data["bc_msg"])
            ok += 1
        except:
            fail += 1
        await asyncio.sleep(0.05)
    await c.message.edit_text(f"✅ Рассылка завершена!\n\n💚 Доставлено: {ok}\n🥀 Не доставлено: {fail}")

# ======================== ТРЕКИНГ АКТИВНОСТИ КАНАЛОВ ========================
@router.channel_post()
async def on_channel_post(message: Message):
    chat_id = message.chat.id
    title = message.chat.title or "Канал"
    touch_channel(chat_id, title)
    today = now_local().strftime("%Y-%m-%d")
    cur.execute("INSERT OR IGNORE INTO chan_stats(chat_id, date) VALUES(?,?)", (chat_id, today))
    cur.execute("UPDATE chan_stats SET posts=posts+1 WHERE chat_id=? AND date=?", (chat_id, today))
    db.commit()

try:
    @router.message_reaction()
    async def on_reaction(event):
        try:
            chat_id = event.chat.id
            today = now_local().strftime("%Y-%m-%d")
            added = len(event.new_reaction or [])
            if added:
                cur.execute("INSERT OR IGNORE INTO chan_stats(chat_id, date) VALUES(?,?)", (chat_id, today))
                cur.execute("UPDATE chan_stats SET reactions=reactions+? WHERE chat_id=? AND date=?",
                            (added, chat_id, today))
                db.commit()
        except:
            pass
except Exception:
    pass  # старая версия aiogram без реакций

# ======================== 🐸 ПЛАНИРОВЩИК-СКАНЕР (страховка) ========================
async def scheduler_tick():
    now_s = now_local().strftime(DB_FMT)

    # 1. Догоняем запланированные посты (страховка, если таймер умер)
    try:
        cur.execute("SELECT id FROM posts WHERE status='scheduled' AND send_at<=?", (now_s,))
        for r in cur.fetchall():
            await publish_post_by_id(r["id"], notify=True)
    except Exception as e:
        log.error(f"Scheduler (posts): {e}")

    # 2. Автоудаление постов
    try:
        cur.execute("SELECT id, chat_id, message_id FROM posts "
                    "WHERE status='published' AND delete_at IS NOT NULL AND delete_at<=?", (now_s,))
        for p in cur.fetchall():
            if p["message_id"]:
                try:
                    await bot.delete_message(p["chat_id"], p["message_id"])
                    log.info(f"[🗑] Пост #{p['id']} удалён")
                except Exception as e:
                    log.warning(f"Не удалить пост #{p['id']}: {e}")
            cur.execute("UPDATE posts SET status='deleted' WHERE id=?", (p["id"],))
            db.commit()
    except Exception as e:
        log.error(f"Scheduler (autodelete): {e}")

    # 3. Публикация конкурсов по времени
    try:
        cur.execute("SELECT id FROM giveaways WHERE status='created' AND publish_at<=?", (now_s,))
        for g in cur.fetchall():
            await publish_giveaway(g["id"])
    except Exception as e:
        log.error(f"Scheduler (gw publish): {e}")

    # 4. Итоги конкурсов по времени
    try:
        cur.execute("SELECT id FROM giveaways WHERE status='running' AND end_type='time' AND end_value<=?",
                    (now_s,))
        for g in cur.fetchall():
            await finish_giveaway(g["id"])
    except Exception as e:
        log.error(f"Scheduler (gw time): {e}")

    # 5. Итоги конкурсов по числу участников
    try:
        cur.execute("SELECT id, end_value FROM giveaways WHERE status='running' AND end_type='count'")
        for g in cur.fetchall():
            cur.execute("SELECT COUNT(*) c FROM participants WHERE giveaway_id=?", (g["id"],))
            if cur.fetchone()["c"] >= int(g["end_value"]):
                await finish_giveaway(g["id"])
    except Exception as e:
        log.error(f"Scheduler (gw count): {e}")

async def scheduler():
    global scheduler_last_tick
    log.info("[⏰] Планировщик-сканер запущен (проверка каждые 5 сек)")
    while True:
        try:
            scheduler_last_tick = datetime.now()
            await scheduler_tick()
        except Exception as e:
            log.error(f"Scheduler CRASH — перезапускаю цикл: {e}")
        await asyncio.sleep(5)

# ======================== ГЛОБАЛЬНЫЙ ЛОВЕЦ ОШИБОК ========================
@dp.error()
async def global_error_handler(event):
    exc = getattr(event, "exception", None)
    log.error(f"ГЛОБАЛЬНАЯ ОШИБКА: {exc}", exc_info=exc)
    try:
        await bot.send_message(ADMIN_ID,
                               f"⚠️ Ошибка в боте:\n{type(exc).__name__}: {str(exc)[:400]}")
    except:
        pass
    return True

# ======================== ЗАПУСК ========================
async def main():
    global BOT_USERNAME, scheduler_task
    init_db()
    me = await bot.get_me()
    BOT_USERNAME = me.username
    scheduler_task = asyncio.create_task(scheduler())  # храним ссылку на задачу!
    log.info(LINE)
    log.info("   🐸 POSTFROG ЗАПУЩЕН 🐸")
    log.info(LINE)
    log.info(f"👑 Админ: {ADMIN_ID}")
    log.info(f"🔗 Бот: @{BOT_USERNAME}")
    log.info(f"🖥 Время сервера: {now_local().strftime(DATE_FMT)} (сдвиг +{TZ_OFFSET} ч.)")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
