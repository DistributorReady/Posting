# -*- coding: utf-8 -*-
# ============================================================
#  TELEGRAM БОТ: АВТОПОСТИНГ + КОНКУРСЫ + ПОДПИСКИ + АДМИНКА
#  Установка: pip install aiogram
# ============================================================
import asyncio
import sqlite3
import random
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup,
                           InlineKeyboardButton, LabeledPrice, PreCheckoutQuery)
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ======================== НАСТРОЙКИ ========================
BOT_TOKEN = "СЮДА_ТОКЕН_БОТА"      # токен от @BotFather
ADMIN_ID = 123456789               # твой Telegram ID (@userinfobot)

DATE_FMT = "%d.%m.%Y %H:%M"        # формат даты для ввода: 01.05.2025 15:30
DB_FMT = "%Y-%m-%d %H:%M:%S"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

BOT_USERNAME = ""  # заполнится автоматически

# ======================== БАЗА ДАННЫХ ========================
db = sqlite3.connect("bot.db", check_same_thread=False)
db.row_factory = sqlite3.Row
cur = db.cursor()

def init_db():
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
    """)
    db.commit()

# ======================== ХЕЛПЕРЫ БД ========================
def get_user(uid):
    cur.execute("SELECT * FROM users WHERE user_id=?", (uid,))
    return cur.fetchone()

def upsert_user(uid, username=None):
    cur.execute("INSERT OR IGNORE INTO users(user_id, username, joined) VALUES(?,?,?)",
                (uid, username, datetime.now().strftime(DB_FMT)))
    cur.execute("UPDATE users SET username=? WHERE user_id=?", (username, uid))
    db.commit()

def add_sub_days(uid, days):
    """Добавляет дни подписки (days=0 -> навсегда)"""
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
    now = datetime.now()
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
        return "⭐ PREMIUM — НАВСЕГДА"
    if u["sub_type"] == "premium" and u["sub_until"]:
        until = datetime.strptime(u["sub_until"], DB_FMT)
        if until > datetime.now():
            return f"⭐ PREMIUM — до {until.strftime('%d.%m.%Y %H:%M')}"
    return "🆓 Free"

def get_ad_text():
    cur.execute("SELECT value FROM settings WHERE key='ad_text'")
    r = cur.fetchone()
    return r["value"] if r else None

def set_ad_text(text):
    cur.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('ad_text', ?)", (text,))
    db.commit()

def del_ad_text():
    cur.execute("DELETE FROM settings WHERE key='ad_text'")
    db.commit()

def save_channel(chat_id, title, owner_id):
    cur.execute("INSERT OR REPLACE INTO channels(chat_id, title, owner_id) VALUES(?,?,?)",
                (chat_id, title, owner_id))
    db.commit()

def user_channels(uid):
    cur.execute("SELECT * FROM channels WHERE owner_id=?", (uid,))
    return cur.fetchall()

def count_user_channels(uid):
    cur.execute("SELECT COUNT(*) c FROM channels WHERE owner_id=?", (uid,))
    return cur.fetchone()["c"]

# ======================== FSM ========================
class AddPost(StatesGroup):
    select_channel = State()
    text = State()
    photo = State()
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

class AdminFSM(StatesGroup):
    promo_code = State()
    promo_days = State()
    give_id = State()
    give_days = State()
    take_id = State()
    ad_text = State()
    broadcast = State()
    broadcast_confirm = State()

# ======================== КЛАВИАТУРЫ ========================
def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Мой профиль", callback_data="profile")],
        [InlineKeyboardButton(text="📝 Добавить пост", callback_data="add_post")],
        [InlineKeyboardButton(text="🎁 Конкурс", callback_data="new_giveaway")],
        [InlineKeyboardButton(text="⭐ Подписка", callback_data="subscription"),
         InlineKeyboardButton(text="🎟 Промокод", callback_data="promo")],
        [InlineKeyboardButton(text="🏆 Мои конкурсы", callback_data="my_giveaways")],
    ])

def back_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")]
    ])

def sub_buy_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="7 дней — 9 ⭐", callback_data="buy_7")],
        [InlineKeyboardButton(text="31 день — 29 ⭐", callback_data="buy_31")],
        [InlineKeyboardButton(text="93 дня — 49 ⭐", callback_data="buy_93")],
        [InlineKeyboardButton(text="Навсегда — 99 ⭐", callback_data="buy_life")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")],
    ])

# ======================== ВСПОМОГАТЕЛЬНОЕ ========================
async def bot_is_admin(chat_id):
    try:
        m = await bot.get_chat_member(chat_id, bot.id)
        return m.status == "administrator"
    except:
        return False

async def user_subscribed(chat_id, user_id):
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        return m.status in ("member", "administrator", "creator")
    except:
        return False

async def extract_chat(message: Message):
    """Достаёт канал из пересланного сообщения или @username. Возвращает (chat_id, title) или None"""
    if message.forward_origin:
        chat = getattr(message.forward_origin, "chat", None)
        if chat:
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

async def send_to_channel(chat_id, text, photo=None):
    """Отправляет пост в канал, добавляя рекламный текст админа"""
    ad = get_ad_text()
    if ad:
        text = text + "\n\n———————\n" + ad
    try:
        if photo:
            msg = await bot.send_photo(chat_id, photo, caption=text[:1024])
        else:
            msg = await bot.send_message(chat_id, text[:4096])
        return msg
    except Exception as e:
        print(f"[!] Ошибка отправки в {chat_id}: {e}")
        return None

# ======================== /start ========================
@router.message(CommandStart(deep_link=True), F.chat.type == "private")
async def start_deep(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    upsert_user(message.from_user.id, message.from_user.username)
    args = command.args.strip()

    # Участие в конкурсе: /start gw_5
    if args.startswith("gw_"):
        try:
            gid = int(args.split("_")[1])
        except:
            return
        await try_join_giveaway(message.from_user.id, gid)
        return

    await message.answer("👋 Главного меню нет по этой ссылке.", reply_markup=back_menu())

@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    upsert_user(message.from_user.id, message.from_user.username)
    await message.answer(
        f"👋 Привет, <b>{message.from_user.first_name}</b>!\n"
        "Это бот автопостинга и конкурсов.\n\nВыбери действие:",
        reply_markup=main_menu()
    )

@router.message(CommandStart())
async def cmd_start_group(message: Message):
    await message.reply("🤖 Напиши мне в личку, чтобы пользоваться ботом.")

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Отменено.", reply_markup=main_menu())

# ======================== МЕНЮ / ПРОФИЛЬ ========================
@router.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("👋 Главное меню:", reply_markup=main_menu())

@router.callback_query(F.data == "profile")
async def cb_profile(c: CallbackQuery):
    u = get_user(c.from_user.id)
    channels = count_user_channels(c.from_user.id)
    text = (
        "👤 <b>Мой профиль</b>\n\n"
        f"🆔 ID: <code>{c.from_user.id}</code>\n"
        f"📛 Username: @{c.from_user.username}\n" if c.from_user.username else "",
        f"⭐ Статус: {sub_text(u)}\n"
        f"📡 Каналов/групп подключено: <b>{channels}</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏆 Мои конкурсы", callback_data="my_giveaways")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")],
    ])
    profile_text = (
        "👤 <b>Мой профиль</b>\n\n"
        f"🆔 ID: <code>{c.from_user.id}</code>\n"
        + (f"📛 Username: @{c.from_user.username}\n" if c.from_user.username else "")
        + f"⭐ Статус: {sub_text(u)}\n"
        + f"📡 Каналов/групп подключено: <b>{channels}</b>"
    )
    await c.message.edit_text(profile_text, reply_markup=kb)

# ======================== ПОДПИСКА (ЗВЁЗДЫ) ========================
@router.callback_query(F.data == "subscription")
async def cb_sub(c: CallbackQuery):
    u = get_user(c.from_user.id)
    await c.message.edit_text(
        f"⭐ <b>Подписка PREMIUM</b>\n\nТвой статус: {sub_text(u)}\n\n"
        "Оплата Telegram-звёздами:",
        reply_markup=sub_buy_kb()
    )

SUB_PLANS = {"buy_7": (7, 9, "PREMIUM 7 дней"), "buy_31": (31, 29, "PREMIUM 31 день"),
             "buy_93": (93, 49, "PREMIUM 93 дня"), "buy_life": (0, 99, "PREMIUM навсегда")}

@router.callback_query(F.data.in_(SUB_PLANS.keys()))
async def cb_buy(c: CallbackQuery):
    days, stars, title = SUB_PLANS[c.data]
    await bot.send_invoice(
        chat_id=c.from_user.id,
        title=f"⭐ {title}",
        description=f"Подписка {title} для бота автопостинга",
        payload=f"sub_{c.data.split('_')[1]}",
        currency="XTR",
        prices=[LabeledPrice(label=title, amount=stars)]
    )

@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)

@router.message(F.successful_payment)
async def on_payment(message: Message):
    payload = message.successful_payment.invoice_payload  # sub_7 / sub_31 / sub_93 / sub_life
    key = payload.replace("sub_", "")
    if key == "life":
        add_sub_days(message.from_user.id, 0)
        await message.answer("✅ Оплата прошла! Тебе выдана подписка <b>PREMIUM НАВСЕГДА</b> 🎉")
    else:
        add_sub_days(message.from_user.id, int(key))
        await message.answer(f"✅ Оплата прошла! Подписка <b>PREMIUM на {key} дн.</b> активирована 🎉")

# ======================== ПРОМОКОД ========================
@router.callback_query(F.data == "promo")
async def cb_promo(c: CallbackQuery, state: FSMContext):
    await state.set_state(PromoIn.code)
    await c.message.edit_text("🎟 Введи промокод:\n\nДля отмены: /cancel")

@router.message(PromoIn.code)
async def process_promo(message: Message, state: FSMContext):
    code = message.text.strip().upper()
    cur.execute("SELECT * FROM promos WHERE code=?", (code,))
    promo = cur.fetchone()
    if not promo:
        await message.answer("❌ Такого промокода не существует. Попробуй ещё:")
        return
    if promo["used_by"]:
        await message.answer("⚠️ Этот промокод уже использован. Введи другой:")
        return
    cur.execute("UPDATE promos SET used_by=? WHERE code=?", (message.from_user.id, code))
    db.commit()
    add_sub_days(message.from_user.id, promo["days"])
    await state.clear()
    if promo["days"] == 0:
        await message.answer("🎉 Промокод активирован! Подписка <b>PREMIUM НАВСЕГДА</b>", reply_markup=back_menu())
    else:
        await message.answer(f"🎉 Промокод активирован! +<b>{promo['days']} дн.</b> PREMIUM", reply_markup=back_menu())

# ======================== ДОБАВЛЕНИЕ ПОСТА ========================
@router.callback_query(F.data == "add_post")
async def cb_add_post(c: CallbackQuery, state: FSMContext):
    await state.clear()
    chans = user_channels(c.from_user.id)
    kb_rows = [[InlineKeyboardButton(text=ch["title"], callback_data=f"pick_ch_{ch['chat_id']}")] for ch in chans]
    kb_rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="menu")])
    await state.set_state(AddPost.select_channel)
    await c.message.edit_text(
        "📝 <b>Добавление поста</b>\n\nВыбери канал для публикации\nили перешли любое сообщение из канала:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows)
    )

@router.callback_query(AddPost.select_channel, F.data.startswith("pick_ch_"))
async def cb_pick_channel(c: CallbackQuery, state: FSMContext):
    chat_id = int(c.data.replace("pick_ch_", ""))
    await state.update_data(post={"chat_id": chat_id, "text": None, "photo": None,
                                  "delete_hours": None, "send_at": None})
    await state.set_state(AddPost.text)
    await c.message.edit_text("✍️ Введи текст поста:\n\nДля отмены: /cancel")

@router.message(AddPost.select_channel)
async def post_pick_forward(message: Message, state: FSMContext):
    chat = await extract_chat(message)
    if not chat:
        await message.answer("⚠️ Не удалось определить канал. Перешли сообщение из канала или введи @username:")
        return
    chat_id, title = chat
    if not await bot_is_admin(chat_id):
        await message.answer("⛔ Добавьте в канал нашего бота для продолжения (с правами администратора).")
        return
    save_channel(chat_id, title, message.from_user.id)
    await state.update_data(post={"chat_id": chat_id, "text": None, "photo": None,
                                  "delete_hours": None, "send_at": None})
    await state.set_state(AddPost.text)
    await message.answer(f"✅ Канал: <b>{title}</b>\n\n✍️ Теперь введи текст поста:")

@router.message(AddPost.text)
async def post_text(message: Message, state: FSMContext):
    data = await state.get_data()
    data["post"]["text"] = message.text
    await state.update_data(post=data["post"])
    await state.set_state(AddPost.photo)
    await message.answer(
        "🖼 Отправь фото для поста или нажми «Пропустить»:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Пропустить", callback_data="post_no_photo")],
        ])
    )

@router.message(AddPost.photo, F.photo)
async def post_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    data["post"]["photo"] = message.photo[-1].file_id
    await state.update_data(post=data["post"])
    await ask_autodelete(message, state)

@router.callback_query(AddPost.photo, F.data == "post_no_photo")
async def post_no_photo(c: CallbackQuery, state: FSMContext):
    await ask_autodelete(c.message, state)

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
    await show_post_confirm(c.message, state, scheduled=False)

@router.callback_query(AddPost.when, F.data == "post_later")
async def post_later(c: CallbackQuery, state: FSMContext):
    await state.set_state(AddPost.schedule_time)
    await c.message.edit_text(f"⏰ Введи дату и время публикации\n\nФормат: {DATE_FMT}\nПример: 25.12.2025 18:00")

@router.message(AddPost.schedule_time)
async def post_time(message: Message, state: FSMContext):
    dt = parse_date(message.text)
    if not dt:
        await message.answer(f"⚠️ Неверный формат. Введи дату как: {DATE_FMT}")
        return
    if dt < datetime.now():
        await message.answer("⚠️ Эта дата уже прошла. Введи будущую дату:")
        return
    data = await state.get_data()
    data["post"]["send_at"] = dt.strftime(DB_FMT)
    await state.update_data(post=data["post"])
    await state.set_state(AddPost.confirm)
    await show_post_confirm(message, state, scheduled=True)

async def show_post_confirm(message, state, scheduled):
    data = await state.get_data()
    p = data["post"]
    when_txt = "📤 Сейчас" if p["send_at"] == "now" else f"⏰ {p['send_at']}"
    del_txt = f"через {p['delete_hours']} ч." if p["delete_hours"] else "нет"
    preview = (p["text"] or "")[:300]
    text = (
        "📋 <b>Проверь пост:</b>\n\n"
        f"{preview}\n\n"
        f"🖼 Фото: {'есть' if p['photo'] else 'нет'}\n"
        f"📅 Отправка: {when_txt}\n"
        f"🗑 Автоудаление: {del_txt}"
    )
    if scheduled:
        kb = [[InlineKeyboardButton(text="📤 Выложить сейчас", callback_data="post_confirm")],
              [InlineKeyboardButton(text="✏️ Редактировать текст", callback_data="post_edit")],
              [InlineKeyboardButton(text="❌ Отменить выкладывание", callback_data="post_cancel_ask")]]
    else:
        kb = [[InlineKeyboardButton(text="✅ Подтвердить выкладывание", callback_data="post_confirm")],
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
    await show_post_confirm(message, state, scheduled)

@router.callback_query(AddPost.confirm, F.data == "post_cancel_ask")
async def post_cancel_ask(c: CallbackQuery):
    await c.message.edit_text(
        "🤔 Точно отменить выкладывание поста?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, отменить", callback_data="post_cancel")],
            [InlineKeyboardButton(text="↩️ Нет, вернуться", callback_data="post_back_confirm")],
        ])
    )

@router.callback_query(AddPost.confirm, F.data == "post_back_confirm")
async def post_back_confirm(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await show_post_confirm(c.message, state, data["post"]["send_at"] != "now")

@router.callback_query(AddPost.confirm, F.data == "post_cancel")
async def post_cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("❌ Пост отменён.", reply_markup=back_menu())

@router.callback_query(AddPost.confirm, F.data == "post_confirm")
async def post_confirm(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    p = data["post"]
    send_at = datetime.now() if p["send_at"] == "now" else datetime.strptime(p["send_at"], DB_FMT)
    cur.execute(
        "INSERT INTO posts(owner_id, chat_id, text, photo, delete_hours, send_at, status) VALUES(?,?,?,?,?,?,?)",
        (c.from_user.id, p["chat_id"], p["text"], p["photo"], p["delete_hours"],
         send_at.strftime(DB_FMT), "scheduled")
    )
    db.commit()
    await state.clear()
    if p["send_at"] == "now":
        await publish_pending_posts()  # сразу публикуем
        await c.message.edit_text("✅ Пост опубликован в канал!", reply_markup=back_menu())
    else:
        await c.message.edit_text(
            f"✅ Пост запланирован на {p['send_at']}",
            reply_markup=back_menu()
        )

# ======================== КОНКУРСЫ ========================
@router.callback_query(F.data == "new_giveaway")
async def cb_new_gw(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.update_data(gw={"channels": []})
    await state.set_state(Giveaway.description)
    await c.message.edit_text(
        "🎁 <b>Создание конкурса</b>\n\nШаг 1/7 — введи описание конкурса\n(что разыгрывается, условия и т.д.):\n\nДля отмены: /cancel"
    )

@router.message(Giveaway.description)
async def gw_description(message: Message, state: FSMContext):
    data = await state.get_data()
    data["gw"]["description"] = message.text
    await state.update_data(gw=data["gw"])
    await state.set_state(Giveaway.photo)
    await message.answer(
        "Шаг 2/7 — отправь фото для конкурса:",
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
    await message.answer("Шаг 3/7 — введи число победителей (от 1 до 100000):")

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
        "Шаг 4/7 — выбери условия конкурса:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить обязательную подписку", callback_data="gw_sub_yes")],
            [InlineKeyboardButton(text="⏭ Пропустить", callback_data="gw_sub_no")],
        ])
    )

@router.callback_query(Giveaway.condition, F.data == "gw_sub_no")
async def gw_sub_no(c: CallbackQuery, state: FSMContext):
    await gw_select_channel(c.message, state)

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
        await message.answer("⚠️ Не удалось определить канал. Перешли сообщение из канала или введи @username:")
        return
    chat_id, title = chat
    if not await bot_is_admin(chat_id):
        await message.answer("⛔ Добавьте в канал нашего бота для продолжения (с правами администратора).")
        return
    data = await state.get_data()
    chans = data["gw"]["channels"]
    if any(ch[0] == chat_id for ch in chans):
        await message.answer("⚠️ Этот канал уже добавлен.")
        return
    chans.append((chat_id, title))
    data["gw"]["channels"] = chans
    await state.update_data(gw=data["gw"])
    lst = "\n".join(f"  {i+1}. {t}" for i, (_, t) in enumerate(chans))
    await message.answer(
        f"✅ Канал <b>{title}</b> успешно добавлен!\n\n📣 Каналы для подписки:\n{lst}",
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
    await gw_select_channel(c.message, state)

async def gw_select_channel(message, state):
    """Выбор канала, куда будет выложен конкурс"""
    data = await state.get_data()
    uid = data.get("uid") or None
    await state.set_state(Giveaway.select_channel)
    chans = user_channels(data["gw"].get("owner_id", 0)) if data["gw"].get("owner_id") else []
    await message.answer(
        "📢 Выбери канал, куда выложить конкурс\nили перешли сообщение из канала:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="menu")],
        ])
    )

@router.message(Giveaway.select_channel)
async def gw_pick_forward(message: Message, state: FSMContext):
    chat = await extract_chat(message)
    if not chat:
        await message.answer("⚠️ Не удалось определить канал. Перешли сообщение из канала или введи @username:")
        return
    chat_id, title = chat
    if not await bot_is_admin(chat_id):
        await message.answer("⛔ Добавьте в канал нашего бота для продолжения (с правами администратора).")
        return
    save_channel(chat_id, title, message.from_user.id)
    data = await state.get_data()
    data["gw"]["channel_id"] = chat_id
    data["gw"]["channel_title"] = title
    data["gw"]["owner_id"] = message.from_user.id
    await state.update_data(gw=data["gw"])
    await state.set_state(Giveaway.end_type)
    await message.answer(
        "Шаг 5/7 — когда подвести итоги конкурса?",
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
    if not dt or dt < datetime.now():
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
        "Шаг 6/7 — когда опубликовать конкурс?",
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
    if not dt or dt < datetime.now():
        await message.answer(f"⚠️ Введи корректную будущую дату ({DATE_FMT}):")
        return
    await save_and_schedule_giveaway(message, state, dt)

@router.callback_query(Giveaway.publish_when, F.data == "gw_pub_now")
async def gw_pub_now(c: CallbackQuery, state: FSMContext):
    await save_and_schedule_giveaway(c.message, state, datetime.now())

async def save_and_schedule_giveaway(message, state, dt):
    data = await state.get_data()
    gw = data["gw"]
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
    if dt <= datetime.now():
        await publish_giveaway(gid)
        await message.answer("🎉 Конкурс опубликован в канале!", reply_markup=back_menu())
    else:
        await message.answer(f"✅ Конкурс запланирован на {dt.strftime(DATE_FMT)}", reply_markup=back_menu())

async def publish_giveaway(gid):
    cur.execute("SELECT * FROM giveaways WHERE id=?", (gid,))
    g = cur.fetchone()
    if not g or g["status"] != "created":
        return
    cur.execute("SELECT * FROM g_channels WHERE giveaway_id=?", (gid,))
    req = cur.fetchall()
    cond = ""
    if req:
        titles = "\n".join(f"  • {r['title']}" for r in req)
        cond = f"\n📌 <b>Условие:</b> подписка на каналы:\n{titles}\n"
    text = (
        f"🎉 <b>КОНКУРС</b> 🎉\n\n"
        f"{g['description']}\n"
        f"\n🏆 Победителей: <b>{g['winners']}</b>"
        f"{cond}"
        f"\n⏳ Итоги: {'по времени — ' + g['end_value'] if g['end_type']=='time' else 'после ' + g['end_value'] + ' участников'}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎉 Участвовать",
                              url=f"https://t.me/{BOT_USERNAME}?start=gw_{gid}")]
    ])
    msg = None
    try:
        if g["photo"]:
            msg = await bot.send_photo(g["channel_id"], g["photo"], caption=text[:1024], reply_markup=kb)
        else:
            msg = await bot.send_message(g["channel_id"], text[:4096], reply_markup=kb)
    except Exception as e:
        print(f"[!] Ошибка публикации конкурса {gid}: {e}")
    cur.execute("UPDATE giveaways SET status='running', post_message_id=? WHERE id=?",
                (msg.message_id if msg else None, gid))
    db.commit()

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
        await bot.send_message(user_id, "😎 Ты уже участвуешь в этом конкурсе!")
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
            "🔔 Сначала подпишись на каналы для участия в конкурсе:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows)
        )
        return
    cur.execute("INSERT INTO participants(giveaway_id, user_id) VALUES(?,?)", (gid, user_id))
    db.commit()
    await bot.send_message(user_id, "🎉 Ты участвуешь в конкурсе!")
    await send_gw_stats(user_id, gid)
    # Проверка завершения по числу участников
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
        delta = end - datetime.now()
        h, m = delta.seconds // 3600, (delta.seconds // 60) % 60
        left = f"⏳ До конца: {delta.days} дн. {h} ч. {m} мин."
    else:
        left = f"⏳ До конца: ещё {max(0, int(g['end_value']) - cnt)} участников"
    await bot.send_message(
        user_id,
        f"📊 <b>Статистика конкурса</b>\n\n"
        f"👥 Участников: <b>{cnt}</b>\n{left}"
    )

# ---------- Мои конкурсы ----------
@router.callback_query(F.data == "my_giveaways")
async def cb_my_gw(c: CallbackQuery):
    cur.execute("SELECT * FROM giveaways WHERE owner_id=? ORDER BY id DESC LIMIT 10", (c.from_user.id,))
    gws = cur.fetchall()
    if not gws:
        await c.message.edit_text("📭 У тебя пока нет конкурсов.", reply_markup=back_menu())
        return
    status_icon = {"created": "🕒", "running": "🟢", "finished": "🏁"}
    text = "🏆 <b>Мои конкурсы:</b>\n\n"
    for g in gws:
        cur.execute("SELECT COUNT(*) c FROM participants WHERE giveaway_id=?", (g["id"],))
        cnt = cur.fetchone()["c"]
        text += (f"{status_icon.get(g['status'], '•')} #{g['id']} — участников: {cnt} "
                 f"— {g['status']}\n")
    await c.message.edit_text(text, reply_markup=back_menu())

# ---------- Завершение конкурса ----------
async def finish_giveaway(gid):
    cur.execute("SELECT * FROM giveaways WHERE id=?", (gid,))
    g = cur.fetchone()
    if not g or g["status"] != "running":
        return
    cur.execute("SELECT user_id FROM participants WHERE giveaway_id=?", (gid,))
    participants = [r["user_id"] for r in cur.fetchall()]
    if not participants:
        text = "🏁 <b>Итоги конкурса</b>\n\n😔 К сожалению, участников не было."
        try:
            await bot.send_message(g["channel_id"], text)
        except:
            pass
        cur.execute("UPDATE giveaways SET status='finished' WHERE id=?", (gid,))
        db.commit()
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
        mentions.append(f'🏆 <a href="tg://user?id={w}">{name}</a>')
    text = (
        "🏁 <b>ИТОГИ КОНКУРСА</b> 🏁\n\n"
        f"🎉 Победители ({len(winners)}):\n\n" + "\n".join(mentions) +
        "\n\n🎊 Поздравляем победителей!"
    )
    try:
        await bot.send_message(g["channel_id"], text)
        if g["post_message_id"]:
            try:
                await bot.edit_message_reply_markup(g["channel_id"], g["post_message_id"],
                                                    reply_markup=None)
            except:
                pass
    except Exception as e:
        print(f"[!] Ошибка итогов конкурса {gid}: {e}")
    cur.execute("UPDATE giveaways SET status='finished' WHERE id=?", (gid,))
    db.commit()

# ======================== /admin ========================
@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Доступ запрещён.")
        return
    await state.clear()
    ad = get_ad_text()
    text = (
        "👑 <b>Админ-панель</b>\n\n"
        f"📢 Текст к постам: {'установлен' if ad else 'не установлен'}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎟 Сделать промокод", callback_data="adm_promo")],
        [InlineKeyboardButton(text="⭐ Выдать подписку", callback_data="adm_give"),
         InlineKeyboardButton(text="🚫 Забрать подписку", callback_data="adm_take")],
        [InlineKeyboardButton(text="📢 Текст к постам", callback_data="adm_ad")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm_stats")],
        [InlineKeyboardButton(text="📨 Рассылка всем", callback_data="adm_bc")],
    ])
    await message.answer(text, reply_markup=kb)

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
        await c.message.edit_text("⭐ Введи ID пользователя, которому выдать подписку:")
    elif act == "take":
        await state.set_state(AdminFSM.take_id)
        await c.message.edit_text("🚫 Введи ID пользователя, у которого забрать подписку:")
    elif act == "ad":
        ad = get_ad_text()
        await c.message.edit_text(
            f"📢 <b>Текущий текст к постам:</b>\n\n{ad or '— не установлен —'}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✏️ Изменить/Добавить", callback_data="adm_ad_edit")],
                [InlineKeyboardButton(text="🗑 Удалить", callback_data="adm_ad_del")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back")],
            ])
        )
    elif act == "ad_edit":
        await state.set_state(AdminFSM.ad_text)
        await c.message.edit_text("📢 Введи текст, который будет добавляться снизу всех постов (реклама):\n\nДля отмены: /cancel")
    elif act == "ad_del":
        del_ad_text()
        await c.message.edit_text("🗑 Текст удалён.")
    elif act == "back":
        await cmd_admin_callback(c, state)
    elif act == "stats":
        cur.execute("SELECT COUNT(*) c FROM users"); users = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) c FROM users WHERE sub_type!='free'"); prem = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) c FROM channels"); chans = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) c FROM giveaways"); gws = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) c FROM posts"); posts = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) c FROM participants"); parts = cur.fetchone()["c"]
        await c.message.edit_text(
            "📊 <b>Статистика бота</b>\n\n"
            f"👥 Пользователей: <b>{users}</b>\n"
            f"⭐ С подпиской: <b>{prem}</b>\n"
            f"📡 Каналов подключено: <b>{chans}</b>\n"
            f"🎁 Конкурсов создано: <b>{gws}</b>\n"
            f"🙋 Участий в конкурсах: <b>{parts}</b>\n"
            f"📝 Постов: <b>{posts}</b>"
        )
    elif act == "bc":
        await state.set_state(AdminFSM.broadcast)
        await c.message.edit_text("📨 Отправь сообщение для рассылки всем пользователям\n(текст, фото, видео — что угодно):")

async def cmd_admin_callback(c: CallbackQuery, state: FSMContext):
    await state.clear()
    ad = get_ad_text()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎟 Сделать промокод", callback_data="adm_promo")],
        [InlineKeyboardButton(text="⭐ Выдать подписку", callback_data="adm_give"),
         InlineKeyboardButton(text="🚫 Забрать подписку", callback_data="adm_take")],
        [InlineKeyboardButton(text="📢 Текст к постам", callback_data="adm_ad")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm_stats")],
        [InlineKeyboardButton(text="📨 Рассылка всем", callback_data="adm_bc")],
    ])
    await c.message.edit_text("👑 <b>Админ-панель</b>", reply_markup=kb)

@router.message(AdminFSM.promo_code)
async def adm_promo_code(message: Message, state: FSMContext):
    await state.update_data(code=message.text.strip().upper())
    await state.set_state(AdminFSM.promo_days)
    await message.answer("⏳ Введи на сколько дней подписка (0 = навсегда):")

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
        await message.answer(f"✅ Промокод <code>{data['code']}</code> создан ({d})")
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
    await message.answer("⏳ Введи сколько дней подписки выдать (0 = навсегда):")

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
    await message.answer(f"✅ Пользователю <code>{data['uid']}</code> выдана подписка ({days if days else 'навсегда'} дн.)")
    try:
        await bot.send_message(data["uid"], "🎉 Тебе выдана подписка PREMIUM администратором!")
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
    await message.answer(f"✅ Подписка у <code>{uid}</code> забрана.")

@router.message(AdminFSM.ad_text)
async def adm_ad_text(message: Message, state: FSMContext):
    set_ad_text(message.text)
    await state.clear()
    await message.answer("✅ Текст к постам сохранён! Теперь он будет добавляться снизу всех постов.")

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
    await c.message.edit_text("📨 Рассылка запущена...")
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
    await c.message.edit_text(f"✅ Рассылка завершена!\n\nДоставлено: {ok}\nНе доставлено: {fail}")

# ======================== ПЛАНИРОВЩИК (ФОНОВЫЙ ЦИКЛ) ========================
async def publish_pending_posts():
    now = datetime.now()
    cur.execute("SELECT * FROM posts WHERE status='scheduled' AND send_at<=?",
                (now.strftime(DB_FMT),))
    for p in cur.fetchall():
        msg = await send_to_channel(p["chat_id"], p["text"] or "", p["photo"])
        delete_at = None
        if p["delete_hours"]:
            delete_at = (now + timedelta(hours=p["delete_hours"])).strftime(DB_FMT)
        cur.execute("UPDATE posts SET status='published', message_id=?, delete_at=? WHERE id=?",
                    (msg.message_id if msg else None, delete_at, p["id"]))
        db.commit()

async def scheduler():
    while True:
        try:
            now = datetime.now()
            now_s = now.strftime(DB_FMT)

            # Публикация отложенных постов
            await publish_pending_posts()

            # Автоудаление постов
            cur.execute("SELECT * FROM posts WHERE status='published' AND delete_at IS NOT NULL AND delete_at<=?",
                        (now_s,))
            for p in cur.fetchall():
                if p["message_id"]:
                    try:
                        await bot.delete_message(p["chat_id"], p["message_id"])
                    except:
                        pass
                cur.execute("UPDATE posts SET status='deleted' WHERE id=?", (p["id"],))
                db.commit()

            # Публикация отложенных конкурсов
            cur.execute("SELECT id FROM giveaways WHERE status='created' AND publish_at<=?", (now_s,))
            for g in cur.fetchall():
                await publish_giveaway(g["id"])

            # Завершение конкурсов по времени
            cur.execute("SELECT * FROM giveaways WHERE status='running' AND end_type='time' AND end_value<=?",
                        (now_s,))
            for g in cur.fetchall():
                await finish_giveaway(g["id"])

        except Exception as e:
            print(f"[!] Scheduler error: {e}")
        await asyncio.sleep(15)

# ======================== ЗАПУСК ========================
async def main():
    global BOT_USERNAME
    init_db()
    me = await bot.get_me()
    BOT_USERNAME = me.username
    asyncio.create_task(scheduler())
    print("=" * 40)
    print("🤖 Бот запущен!")
    print(f"👑 Админ: {ADMIN_ID}")
    print(f"🔗 Бот: @{BOT_USERNAME}")
    print("=" * 40)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
