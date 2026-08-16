import os
import sqlite3
import threading
import random
from datetime import datetime, timedelta

import telebot
from telebot import types

# =============================================================
# SOZLAMALAR
# =============================================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8832515998:AAHoDmIc97KzOKOj65IFowqGPo9KAUQpHVY")

# Bosh admin(lar) — botni to'liq nazorat qiladi, shaffof ishlaydi
ADMIN_IDS = {8243491785}  # <-- O'ZGARTIRING!

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "database.db")

RANDOM_SEARCH_TIMEOUT_HOURS = 24
MIN_BIRTH_YEAR = 1945
MAX_BIRTH_YEAR = datetime.now().year  # chegara doim joriy yilga teng

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode=None)
BOT_ID = None  # __main__ ichida to'ldiriladi

# Xotiradagi vaqtinchalik holat (FSM o'rniga)
pending_action = {}
PENDING_LOCK = threading.Lock()

DB_LOCK = threading.Lock()

GENDER_LABELS = {"erkak": "Yigit", "ayol": "Qiz"}

# =============================================================
# BAZA
# =============================================================


def get_conn():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with DB_LOCK, get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                telegram_username TEXT,
                profile_name TEXT,
                gender TEXT,
                birth_year INTEGER,
                profile_complete INTEGER DEFAULT 0,
                daily_count INTEGER DEFAULT 0,
                last_count_date TEXT,
                last_search_time TEXT,
                partner_id INTEGER,
                created_at TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS blocked (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                blocked_user_id INTEGER NOT NULL,
                anon_label INTEGER NOT NULL,
                created_at TEXT,
                UNIQUE(user_id, blocked_user_id)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS forced_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_identifier TEXT NOT NULL,
                title TEXT,
                added_by INTEGER,
                created_at TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS broadcast_chats (
                chat_id INTEGER PRIMARY KEY,
                title TEXT,
                chat_type TEXT,
                added_at TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                requester_id INTEGER,
                target_id INTEGER,
                status TEXT,
                created_at TEXT
            )
            """
        )
        conn.commit()

        # Eski (avvalgi versiyadagi) database.db bilan ham ishlashi uchun
        # yetishmayotgan ustunlarni avtomatik qo'shib qo'yamiz.
        cur.execute("PRAGMA table_info(users)")
        existing_cols = {row["name"] for row in cur.fetchall()}
        needed_cols = {
            "telegram_username": "TEXT",
            "profile_name": "TEXT",
            "gender": "TEXT",
            "birth_year": "INTEGER",
            "profile_complete": "INTEGER DEFAULT 0",
            "daily_count": "INTEGER DEFAULT 0",
            "last_count_date": "TEXT",
            "last_search_time": "TEXT",
            "partner_id": "INTEGER",
            "created_at": "TEXT",
            "is_active": "INTEGER DEFAULT 1",
        }
        for col, coltype in needed_cols.items():
            if col not in existing_cols:
                try:
                    cur.execute(f"ALTER TABLE users ADD COLUMN {col} {coltype}")
                except Exception:
                    pass
        conn.commit()


# ---------- Foydalanuvchi / profil ----------


def get_or_create_user(user_id, username, first_name):
    with DB_LOCK, get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "INSERT INTO users (user_id, telegram_username, daily_count, last_count_date, created_at) "
                "VALUES (?, ?, 0, ?, ?)",
                (user_id, username, datetime.now().strftime("%Y-%m-%d"), datetime.now().isoformat()),
            )
            conn.commit()
            return True
        else:
            cur.execute("UPDATE users SET telegram_username=? WHERE user_id=?", (username, user_id))
            conn.commit()
            return False


def get_user(user_id):
    with DB_LOCK, get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        return cur.fetchone()


def user_exists(user_id):
    return get_user(user_id) is not None


def is_profile_complete(user_id):
    row = get_user(user_id)
    return bool(row and row["profile_complete"] == 1)


def set_profile_name(user_id, name):
    with DB_LOCK, get_conn() as conn:
        conn.execute("UPDATE users SET profile_name=? WHERE user_id=?", (name, user_id))
        conn.commit()


def set_profile_gender(user_id, gender):
    with DB_LOCK, get_conn() as conn:
        conn.execute("UPDATE users SET gender=? WHERE user_id=?", (gender, user_id))
        conn.commit()


def set_profile_birth_year(user_id, year):
    with DB_LOCK, get_conn() as conn:
        conn.execute("UPDATE users SET birth_year=? WHERE user_id=?", (year, user_id))
        conn.commit()


def mark_profile_complete(user_id):
    with DB_LOCK, get_conn() as conn:
        conn.execute("UPDATE users SET profile_complete=1 WHERE user_id=?", (user_id,))
        conn.commit()


# ---------- Kunlik limit ----------


def _reset_daily_if_needed(cur, user_id):
    today = datetime.now().strftime("%Y-%m-%d")
    cur.execute("SELECT last_count_date FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if row and row["last_count_date"] != today:
        cur.execute("UPDATE users SET daily_count=0, last_count_date=? WHERE user_id=?", (today, user_id))


def get_daily_count(user_id):
    with DB_LOCK, get_conn() as conn:
        cur = conn.cursor()
        _reset_daily_if_needed(cur, user_id)
        conn.commit()
        cur.execute("SELECT daily_count FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        return row["daily_count"] if row else 0


def increment_daily_count(user_id):
    with DB_LOCK, get_conn() as conn:
        cur = conn.cursor()
        _reset_daily_if_needed(cur, user_id)
        cur.execute("UPDATE users SET daily_count = daily_count + 1 WHERE user_id=?", (user_id,))
        conn.commit()


# ---------- Bloklash ----------


def is_blocked(blocker_id, blocked_id):
    with DB_LOCK, get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM blocked WHERE user_id=? AND blocked_user_id=?", (blocker_id, blocked_id))
        return cur.fetchone() is not None


def add_block(user_id, blocked_user_id):
    with DB_LOCK, get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as c FROM blocked WHERE user_id=?", (user_id,))
        anon_label = cur.fetchone()["c"] + 1
        try:
            cur.execute(
                "INSERT INTO blocked (user_id, blocked_user_id, anon_label, created_at) VALUES (?, ?, ?, ?)",
                (user_id, blocked_user_id, anon_label, datetime.now().isoformat()),
            )
            conn.commit()
            return anon_label
        except sqlite3.IntegrityError:
            return None


def remove_block(user_id, blocked_user_id):
    with DB_LOCK, get_conn() as conn:
        conn.execute("DELETE FROM blocked WHERE user_id=? AND blocked_user_id=?", (user_id, blocked_user_id))
        conn.commit()


def get_block_list(user_id):
    with DB_LOCK, get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT blocked_user_id, anon_label FROM blocked WHERE user_id=? ORDER BY anon_label", (user_id,)
        )
        return cur.fetchall()


def mark_user_inactive(user_id):
    with DB_LOCK, get_conn() as conn:
        conn.execute("UPDATE users SET is_active=0 WHERE user_id=?", (user_id,))
        conn.commit()


def mark_user_active(user_id):
    with DB_LOCK, get_conn() as conn:
        conn.execute("UPDATE users SET is_active=1 WHERE user_id=?", (user_id,))
        conn.commit()


def is_blocked_error(exception):
    text = str(exception).lower()
    return any(k in text for k in ("blocked", "chat not found", "deactivated", "forbidden", "kicked", "bot was kicked"))


def get_user_stats():
    with DB_LOCK, get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as c FROM users")
        total = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) as c FROM users WHERE is_active=1 OR is_active IS NULL")
        active = cur.fetchone()["c"]
        return total, active


def get_all_user_ids():
    with DB_LOCK, get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users")
        return [row["user_id"] for row in cur.fetchall()]


def refresh_user_statuses():
    """Xabar ko'rinishida hech narsa yubormasdan ('typing...' signali orqali)
    har bir foydalanuvchi botni bloklagan-blokdamaganini jonli tekshiradi va
    bazadagi is_active holatini yangilaydi. Foydalanuvchiga hech narsa ko'rinmaydi."""
    for uid in get_all_user_ids():
        try:
            bot.send_chat_action(uid, "typing")
            mark_user_active(uid)
        except Exception as e:
            if is_blocked_error(e):
                mark_user_inactive(uid)
            # boshqa vaqtinchalik xatolarda (masalan tarmoq) holatni o'zgartirmaymiz


def get_all_active_user_ids():
    with DB_LOCK, get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE is_active=1 OR is_active IS NULL")
        return [row["user_id"] for row in cur.fetchall()]


# ---------- Admin ----------


def is_admin(user_id):
    return user_id in ADMIN_IDS


# ---------- Majburiy azolik (kanallar) ----------


def add_forced_channel(identifier, title, added_by):
    with DB_LOCK, get_conn() as conn:
        conn.execute(
            "INSERT INTO forced_channels (chat_identifier, title, added_by, created_at) VALUES (?, ?, ?, ?)",
            (identifier, title, added_by, datetime.now().isoformat()),
        )
        conn.commit()


def list_forced_channels():
    with DB_LOCK, get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM forced_channels ORDER BY id")
        return cur.fetchall()


def remove_forced_channel(channel_id):
    with DB_LOCK, get_conn() as conn:
        conn.execute("DELETE FROM forced_channels WHERE id=?", (channel_id,))
        conn.commit()


def normalize_channel_identifier(raw_text):
    """https://t.me/name, t.me/name, @name, name -> '@name' formatiga keltiradi.
    Shaxsiy taklif havolalari (t.me/+xxxx) qo'llab-quvvatlanmaydi."""
    text = raw_text.strip()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
            break
    text = text.strip("/").strip()
    if not text or text.startswith("+") or text.startswith("joinchat"):
        return None
    if text.startswith("@"):
        text = text[1:]
    if not text:
        return None
    return "@" + text


def is_bot_admin_in_chat(identifier):
    if BOT_ID is None:
        return False
    try:
        member = bot.get_chat_member(identifier, BOT_ID)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


def get_missing_subscriptions(user_id):
    """Foydalanuvchi azo bo'lmagan majburiy kanallar ro'yxatini qaytaradi."""
    missing = []
    for ch in list_forced_channels():
        try:
            member = bot.get_chat_member(ch["chat_identifier"], user_id)
            if member.status in ("left", "kicked"):
                missing.append(ch)
        except Exception:
            # Bot kanalda admin bo'lmasa ham, foydalanuvchiga xalaqit bermaslik uchun o'tkazib yuboramiz
            continue
    return missing


# ---------- Xabar tarqatish uchun guruh/kanallar ro'yxati ----------


def upsert_broadcast_chat(chat_id, title, chat_type):
    with DB_LOCK, get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO broadcast_chats (chat_id, title, chat_type, added_at) VALUES (?, ?, ?, ?)",
            (chat_id, title, chat_type, datetime.now().isoformat()),
        )
        conn.commit()


def remove_broadcast_chat(chat_id):
    with DB_LOCK, get_conn() as conn:
        conn.execute("DELETE FROM broadcast_chats WHERE chat_id=?", (chat_id,))
        conn.commit()


def list_broadcast_chats():
    with DB_LOCK, get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM broadcast_chats")
        return cur.fetchall()


# ---------- Random qidiruv holati ----------


def set_random_wait(user_id, target_id):
    with DB_LOCK, get_conn() as conn:
        conn.execute(
            "UPDATE users SET last_search_time=?, partner_id=? WHERE user_id=?",
            (datetime.now().isoformat(), target_id, user_id),
        )
        conn.commit()


def clear_random_wait(user_id):
    with DB_LOCK, get_conn() as conn:
        conn.execute("UPDATE users SET last_search_time=NULL, partner_id=NULL WHERE user_id=?", (user_id,))
        conn.commit()


def get_random_wait_status(user_id):
    row = get_user(user_id)
    if not row or not row["last_search_time"] or not row["partner_id"]:
        return False, 0
    last_time = datetime.fromisoformat(row["last_search_time"])
    deadline = last_time + timedelta(hours=RANDOM_SEARCH_TIMEOUT_HOURS)
    remaining = (deadline - datetime.now()).total_seconds()
    if remaining <= 0:
        clear_random_wait(user_id)
        return False, 0
    return True, int(remaining)


def find_random_partner(user_id, exclude_ids=None):
    """Faqat qarama-qarshi jinsdan, bloklanmagan, profili to'liq odamni topadi.
    Bitta odamga bir vaqtda bir nechta odamdan taklif kelishi mumkin — shuning
    uchun allaqachon boshqasidan taklif kutayotgan foydalanuvchilar cheklanmaydi."""
    me = get_user(user_id)
    if not me or not me["gender"]:
        return None
    opposite = "ayol" if me["gender"] == "erkak" else "erkak"
    exclude_ids = exclude_ids or set()
    with DB_LOCK, get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT user_id FROM users
            WHERE gender = ?
              AND profile_complete = 1
              AND user_id != ?
              AND (is_active = 1 OR is_active IS NULL)
              AND user_id NOT IN (SELECT blocked_user_id FROM blocked WHERE user_id=?)
              AND user_id NOT IN (SELECT user_id FROM blocked WHERE blocked_user_id=?)
            """,
            (opposite, user_id, user_id, user_id),
        )
        candidates = [r["user_id"] for r in cur.fetchall() if r["user_id"] not in exclude_ids]
        if not candidates:
            return None
        return random.choice(candidates)


def find_reachable_random_partner(user_id, max_attempts=15):
    """find_random_partner natijasini jimgina ('typing...' orqali, xabarsiz)
    tekshirib, haqiqatan yetib boradigan (bloklamagan) birinchi nomzodni qaytaradi.
    Yo'lda uchragan bloklaganlarni bazada ham darhol yangilab qo'yadi."""
    tried = set()
    for _ in range(max_attempts):
        candidate = find_random_partner(user_id, exclude_ids=tried)
        if candidate is None:
            return None
        try:
            bot.send_chat_action(candidate, "typing")
            mark_user_active(candidate)
            return candidate
        except Exception as e:
            tried.add(candidate)
            if is_blocked_error(e):
                mark_user_inactive(candidate)
    return None


# ---------- Tanishuv so'rovlari (pending_matches) ----------


def create_pending_match(requester_id, target_id, status):
    with DB_LOCK, get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO pending_matches (requester_id, target_id, status, created_at) VALUES (?, ?, ?, ?)",
            (requester_id, target_id, status, datetime.now().isoformat()),
        )
        conn.commit()
        return cur.lastrowid


def get_pending_match(match_id):
    with DB_LOCK, get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM pending_matches WHERE id=?", (match_id,))
        return cur.fetchone()


def update_match_status(match_id, status):
    with DB_LOCK, get_conn() as conn:
        conn.execute("UPDATE pending_matches SET status=? WHERE id=?", (status, match_id))
        conn.commit()


# =============================================================
# YORDAMCHI FUNKSIYALAR
# =============================================================


def clear_pending(user_id):
    with PENDING_LOCK:
        pending_action.pop(user_id, None)


def set_pending(user_id, data):
    with PENDING_LOCK:
        pending_action[user_id] = data


def get_pending(user_id):
    with PENDING_LOCK:
        return pending_action.get(user_id)


def format_remaining(seconds):
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"{hours} soat {minutes} daqiqa"


def profile_age(row):
    if row["birth_year"]:
        return datetime.now().year - row["birth_year"]
    return "?"


def escape_html(text):
    if not text:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def profile_card_text(row, show_identity=False):
    name = escape_html(row["profile_name"])
    if show_identity:
        # Ism bosilganda darhol profilga o'tadigan link (username bor-yo'qligidan qat'i nazar ishlaydi)
        name_line = f"👤 Ism: <a href=\"tg://user?id={row['user_id']}\">{name}</a>"
    else:
        name_line = f"👤 Ism: {name}"

    text = (
        f"{name_line}\n"
        f"🚻 Jins: {GENDER_LABELS.get(row['gender'], '?')}\n"
        f"🎂 Yosh: {profile_age(row)}"
    )
    if show_identity:
        text += f"\n🆔 ID: {row['user_id']}"
        if row["telegram_username"]:
            text += f"\n🔗 Username: @{row['telegram_username']}"
    return text


# =============================================================
# KLAVIATURALAR
# =============================================================


def main_menu_keyboard(user_id):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🆔 ID orqali yozish", "🎲 Random qidirish")
    kb.row("👤 Profil", "⚙️ Sozlamalar / Bloklanganlar")
    if is_admin(user_id):
        kb.row("🛠 Admin panel")
    return kb


def cancel_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("❌ Bekor qilish")
    return kb


def admin_menu_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("📢 Kanal qo'shish", "📋 Ulangan kanallar")
    kb.row("📣 Xabar tarqatish", "📊 Statistika")
    kb.row("⬅️ Orqaga")
    return kb


def channels_list_keyboard():
    channels = list_forced_channels()
    if not channels:
        return None
    kb = types.InlineKeyboardMarkup()
    for ch in channels:
        kb.add(
            types.InlineKeyboardButton(
                f"🗑 {ch['title'] or ch['chat_identifier']}",
                callback_data=f"rmchannel_ask|{ch['id']}",
            )
        )
    return kb


def gender_inline_keyboard(prefix):
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("🙂 Yigitman", callback_data=f"{prefix}|erkak"),
        types.InlineKeyboardButton("🙂 Qizman", callback_data=f"{prefix}|ayol"),
    )
    return kb


def profile_edit_keyboard():
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("✏️ Ismni o'zgartirish", callback_data="editname"))
    kb.row(types.InlineKeyboardButton("✏️ Jinsni o'zgartirish", callback_data="editgender_ask"))
    kb.row(types.InlineKeyboardButton("✏️ Tug'ilgan yilni o'zgartirish", callback_data="edityear"))
    return kb


def message_action_keyboard(sender_id, mode, hide_block=False):
    kb = types.InlineKeyboardMarkup()
    buttons = []
    if not hide_block:
        buttons.append(types.InlineKeyboardButton("🚫 Bloklash", callback_data=f"block|{sender_id}"))
    buttons.append(types.InlineKeyboardButton("✍️ Javob yozish", callback_data=f"reply|{sender_id}|{mode}"))
    kb.row(*buttons)
    return kb


def blocked_list_keyboard(user_id):
    rows = get_block_list(user_id)
    if not rows:
        return None
    kb = types.InlineKeyboardMarkup()
    for row in rows:
        kb.add(
            types.InlineKeyboardButton(
                f"🚫 Foydalanuvchi #{row['anon_label']} — blokdan chiqarish",
                callback_data=f"unblock|{row['blocked_user_id']}",
            )
        )
    return kb


# =============================================================
# PROFIL SOZLASH OQIMI
# =============================================================


def start_profile_setup(chat_id, user_id):
    set_pending(user_id, {"step": "awaiting_name"})
    bot.send_message(chat_id, "Avval tanishib olaylik 🙂\nIsmingizni yozing:", reply_markup=types.ReplyKeyboardRemove())


def ask_gender(chat_id, user_id):
    set_pending(user_id, {"step": "awaiting_gender"})
    bot.send_message(chat_id, "Iltimos, quyidagilardan birini tanlang:", reply_markup=gender_inline_keyboard("setupgender"))


def ask_birth_year(chat_id, user_id):
    set_pending(user_id, {"step": "awaiting_birth_year"})
    bot.send_message(chat_id, "Necha yilda tug'ilgansiz? (Masalan: 2000)")


# =============================================================
# /start
# =============================================================


@bot.message_handler(commands=["start"])
def handle_start(message):
    user_id = message.from_user.id
    username = message.from_user.username
    clear_pending(user_id)
    get_or_create_user(user_id, username, message.from_user.first_name or "")
    mark_user_active(user_id)  # bot qayta ishga tushirilgan bo'lsa ("start" bosilsa) statistikada yana faol bo'ladi

    if not is_profile_complete(user_id):
        bot.send_message(
            message.chat.id,
            "👋 Xush kelibsiz!\n\nBotdan foydalanishdan oldin qisqacha tanishib olamiz.",
        )
        start_profile_setup(message.chat.id, user_id)
        return

    bot.send_message(
        message.chat.id,
        "👋 Yana xush kelibsiz!\n\n"
        "🆔 — ID orqali istalgan odamga anonim xabar yozing\n"
        "🎲 — Qarama-qarshi jinsdagi suhbatdosh bilan tasodifiy tanishing\n\n"
        "Menyudan birini tanlang 👇",
        reply_markup=main_menu_keyboard(user_id),
    )


# =============================================================
# ASOSIY MENYU
# =============================================================


@bot.message_handler(func=lambda m: m.text == "❌ Bekor qilish")
def handle_cancel(message):
    clear_pending(message.from_user.id)
    bot.send_message(message.chat.id, "Bekor qilindi.", reply_markup=main_menu_keyboard(message.from_user.id))


@bot.message_handler(func=lambda m: m.text == "⬅️ Orqaga")
def handle_back(message):
    clear_pending(message.from_user.id)
    bot.send_message(message.chat.id, "Bosh menyu:", reply_markup=main_menu_keyboard(message.from_user.id))


@bot.message_handler(func=lambda m: m.text == "👤 Profil")
def handle_profile(message):
    user_id = message.from_user.id
    row = get_user(user_id)
    if not row or not row["profile_complete"]:
        start_profile_setup(message.chat.id, user_id)
        return
    bot.send_message(
        message.chat.id,
        "👤 Sizning profilingiz:\n\n" + profile_card_text(row),
        reply_markup=profile_edit_keyboard(),
    )


@bot.message_handler(func=lambda m: m.text == "🆔 ID orqali yozish")
def handle_id_message_start(message):
    user_id = message.from_user.id
    if not is_profile_complete(user_id):
        start_profile_setup(message.chat.id, user_id)
        return
    missing = get_missing_subscriptions(user_id)
    if missing:
        set_pending(user_id, {"step": "awaiting_subscription", "next": "id"})
        send_subscription_prompt(message.chat.id, missing)
        return
    set_pending(user_id, {"step": "awaiting_target_id"})
    bot.send_message(
        message.chat.id, "🆔 Xabar yubormoqchi bo'lgan odamning Telegram ID raqamini kiriting:", reply_markup=cancel_keyboard()
    )


@bot.message_handler(func=lambda m: m.text == "🎲 Random qidirish")
def handle_random_search(message):
    user_id = message.from_user.id
    if not is_profile_complete(user_id):
        start_profile_setup(message.chat.id, user_id)
        return
    missing = get_missing_subscriptions(user_id)
    if missing:
        set_pending(user_id, {"step": "awaiting_subscription", "next": "random"})
        send_subscription_prompt(message.chat.id, missing)
        return
    start_random_search(message.chat.id, user_id)


def start_random_search(chat_id, user_id):
    row = get_user(user_id)
    if not row["gender"]:
        bot.send_message(chat_id, "Avval profilingizni to'ldirishingiz kerak.")
        start_profile_setup(chat_id, user_id)
        return

    waiting, remaining = get_random_wait_status(user_id)
    if waiting:
        bot.send_message(
            chat_id,
            "⏳ Sizning oldingi so'rovingiz hali javobsiz.\n"
            f"Yangi qidiruv uchun taxminan {format_remaining(remaining)} kutishingiz kerak,\n"
            "yoki suhbatdoshingiz javob berishi bilan cheklov avtomatik olib tashlanadi.",
            reply_markup=main_menu_keyboard(user_id),
        )
        return

    wait_msg = bot.send_message(chat_id, "🔎 Qidirilmoqda...")
    partner_id = find_reachable_random_partner(user_id)
    try:
        bot.delete_message(chat_id, wait_msg.message_id)
    except Exception:
        pass

    if partner_id is None:
        bot.send_message(
            chat_id,
            "😔 Hozircha mos suhbatdosh topilmadi. Birozdan so'ng qayta urinib ko'ring.",
            reply_markup=main_menu_keyboard(user_id),
        )
        return

    match_id = create_pending_match(user_id, partner_id, "preview")
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("✅ Yuborish", callback_data=f"rndconfirm|{match_id}"),
        types.InlineKeyboardButton("❌ Bekor qilish", callback_data=f"rndcancel|{match_id}"),
    )

    if is_admin(user_id):
        # Admin uchun: topilgan foydalanuvchining ma'lumotlari to'g'ridan-to'g'ri ko'rsatiladi.
        # Faqat "Yuborish" bosilsagina o'zining ma'lumotlari narigi tomonga ketadi.
        target_row = get_user(partner_id)
        bot.send_message(
            chat_id,
            "🔎 Topilgan foydalanuvchi:\n\n" + profile_card_text(target_row, show_identity=True) +
            "\n\nUnga tanishuv so'rovi yubormoqchimisiz?",
            reply_markup=kb,
            parse_mode="HTML",
        )
    else:
        bot.send_message(
            chat_id,
            "🎲 Suhbatdosh topildi!\n\n"
            "Unga quyidagi ma'lumotlaringiz yuboriladi:\n\n" + profile_card_text(row) +
            "\n\nYubormoqchimisiz?",
            reply_markup=kb,
        )


@bot.message_handler(func=lambda m: m.text == "⚙️ Sozlamalar / Bloklanganlar")
def handle_settings(message):
    user_id = message.from_user.id
    kb = blocked_list_keyboard(user_id)
    if kb is None:
        bot.send_message(message.chat.id, "🚫 Bloklangan foydalanuvchilar yo'q.", reply_markup=main_menu_keyboard(user_id))
        return
    bot.send_message(message.chat.id, "🚫 Bloklanganlar ro'yxati (anonim raqamlar bilan ko'rsatilgan):", reply_markup=kb)


@bot.message_handler(func=lambda m: m.text == "🛠 Admin panel" and is_admin(m.from_user.id))
def handle_admin_panel(message):
    bot.send_message(message.chat.id, "🛠 Admin panel:", reply_markup=admin_menu_keyboard())


@bot.message_handler(func=lambda m: m.text == "📢 Kanal qo'shish" and is_admin(m.from_user.id))
def handle_add_channel_start(message):
    set_pending(message.from_user.id, {"step": "admin_awaiting_channel"})
    bot.send_message(
        message.chat.id,
        "Majburiy azolik uchun kanal/guruh username'ini yoki linkini yuboring\n"
        "(masalan: @mychannel yoki https://t.me/mychannel).\n\n"
        "⚠️ Eslatma: botni avval o'sha kanal/guruhga ADMIN qilib qo'shing, aks holda qabul qilinmaydi.",
        reply_markup=cancel_keyboard(),
    )


@bot.message_handler(func=lambda m: m.text == "📋 Ulangan kanallar" and is_admin(m.from_user.id))
def handle_list_channels(message):
    kb = channels_list_keyboard()
    if kb is None:
        bot.send_message(message.chat.id, "📋 Hozircha ulangan majburiy kanal yo'q.", reply_markup=admin_menu_keyboard())
        return
    bot.send_message(message.chat.id, "📋 Ulangan majburiy kanallar (o'chirish uchun bosing):", reply_markup=kb)


@bot.message_handler(func=lambda m: m.text == "📣 Xabar tarqatish" and is_admin(m.from_user.id))
def handle_broadcast_start(message):
    set_pending(message.from_user.id, {"step": "admin_awaiting_broadcast_text"})
    bot.send_message(
        message.chat.id,
        "Tarqatmoqchi bo'lgan xabar matnini yuboring — botga start bosgan barcha foydalanuvchilarga yuboriladi:",
        reply_markup=cancel_keyboard(),
    )


@bot.message_handler(func=lambda m: m.text == "📊 Statistika" and is_admin(m.from_user.id))
def handle_stats(message):
    wait_msg = bot.send_message(message.chat.id, "⏳ Tekshirilmoqda...")
    refresh_user_statuses()
    total, active = get_user_stats()
    blocked = total - active
    try:
        bot.delete_message(message.chat.id, wait_msg.message_id)
    except Exception:
        pass
    bot.send_message(
        message.chat.id,
        "📊 Bot statistikasi:\n\n"
        f"👥 Jami ro'yxatdan o'tganlar: {total}\n"
        f"✅ Faol (botni bloklamagan): {active}\n"
        f"🚫 Botni bloklaganlar: {blocked}",
        reply_markup=admin_menu_keyboard(),
    )


# =============================================================
# MAJBURIY AZOLIK
# =============================================================


def send_subscription_prompt(chat_id, missing_channels):
    kb = types.InlineKeyboardMarkup()
    for ch in missing_channels:
        identifier = ch["chat_identifier"]
        display_title = ch["title"] or identifier
        if identifier.startswith("@"):
            url = f"https://t.me/{identifier[1:]}"
            kb.row(types.InlineKeyboardButton(f"📢 {display_title}", url=url))
    kb.row(types.InlineKeyboardButton("✅ Tekshirish", callback_data="checksub"))
    bot.send_message(
        chat_id,
        "Botdan foydalanish uchun avval quyidagi kanal(lar)ga a'zo bo'lishingiz kerak:",
        reply_markup=kb,
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("rmchannel_ask|"))
def handle_rmchannel_ask(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Ruxsat yo'q.")
        return
    channel_id = int(call.data.split("|")[1])
    bot.answer_callback_query(call.id)
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("✅ Ha, o'chirish", callback_data=f"rmchannel_confirm|{channel_id}"),
        types.InlineKeyboardButton("❌ Yo'q", callback_data="rmchannel_cancel"),
    )
    bot.send_message(call.message.chat.id, "Ushbu kanalni majburiy azolik ro'yxatidan chiqarmoqchimisiz?", reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("rmchannel_confirm|"))
def handle_rmchannel_confirm(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Ruxsat yo'q.")
        return
    channel_id = int(call.data.split("|")[1])
    remove_forced_channel(channel_id)
    bot.answer_callback_query(call.id, "✅ O'chirildi.")
    try:
        bot.edit_message_text("✅ Kanal ro'yxatdan chiqarildi.", call.message.chat.id, call.message.message_id)
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data == "rmchannel_cancel")
def handle_rmchannel_cancel(call):
    bot.answer_callback_query(call.id, "Bekor qilindi.")
    try:
        bot.edit_message_text("Bekor qilindi.", call.message.chat.id, call.message.message_id)
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data == "checksub")
def handle_checksub(call):
    user_id = call.from_user.id
    missing = get_missing_subscriptions(user_id)
    if missing:
        bot.answer_callback_query(call.id, "Hali barcha kanallarga a'zo bo'lmadingiz.")
        return
    bot.answer_callback_query(call.id, "✅ Tasdiqlandi!")
    state = get_pending(user_id) or {}
    next_action = state.get("next")
    clear_pending(user_id)
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    if next_action == "id":
        set_pending(user_id, {"step": "awaiting_target_id"})
        bot.send_message(
            call.message.chat.id,
            "🆔 Xabar yubormoqchi bo'lgan odamning Telegram ID raqamini kiriting:",
            reply_markup=cancel_keyboard(),
        )
    elif next_action == "random":
        start_random_search(call.message.chat.id, user_id)
    else:
        bot.send_message(call.message.chat.id, "Bosh menyu:", reply_markup=main_menu_keyboard(user_id))


# =============================================================
# MATNLI XABARLAR (FSM bosqichlari)
# =============================================================


@bot.message_handler(func=lambda m: True, content_types=["text"])
def handle_text(message):
    user_id = message.from_user.id
    if not user_exists(user_id):
        get_or_create_user(user_id, message.from_user.username, message.from_user.first_name or "")

    state = get_pending(user_id)
    if state is None:
        return

    step = state.get("step")

    if step == "awaiting_name":
        _handle_profile_name(message, user_id, edit=False)
    elif step == "edit_name":
        _handle_profile_name(message, user_id, edit=True)
    elif step == "awaiting_birth_year":
        _handle_birth_year(message, user_id, edit=False)
    elif step == "edit_birth_year":
        _handle_birth_year(message, user_id, edit=True)
    elif step == "awaiting_target_id":
        _process_target_id_input(message, user_id)
    elif step == "awaiting_message_text":
        _process_id_message_text(message, user_id, state)
    elif step == "awaiting_reply_text":
        _process_reply_text(message, user_id, state)
    elif step == "admin_awaiting_channel":
        _process_admin_add_channel(message, user_id)
    elif step == "admin_awaiting_broadcast_text":
        _process_admin_broadcast(message, user_id)
    # "awaiting_gender" / "awaiting_subscription" / "confirm_*" bosqichlari inline tugmalar orqali hal qilinadi


def is_valid_name(name):
    if not (2 <= len(name) <= 30):
        return False
    allowed_extra = set("'ʻʼ`-. ")
    return all(ch.isalpha() or ch in allowed_extra for ch in name)


def _handle_profile_name(message, user_id, edit):
    name = message.text.strip()
    if not is_valid_name(name):
        bot.send_message(message.chat.id, "⚠️ Ism faqat harflardan iborat bo'lsin, raqam va belgilarsiz.")
        return
    set_profile_name(user_id, name)
    clear_pending(user_id)
    if edit:
        bot.send_message(message.chat.id, "✅ Ismingiz yangilandi.", reply_markup=main_menu_keyboard(user_id))
    else:
        ask_gender(message.chat.id, user_id)


def _handle_birth_year(message, user_id, edit):
    text = message.text.strip()
    if not text.isdigit() or not (MIN_BIRTH_YEAR <= int(text) <= MAX_BIRTH_YEAR):
        bot.send_message(message.chat.id, f"⚠️ Iltimos, to'g'ri yil kiriting ({MIN_BIRTH_YEAR}-{MAX_BIRTH_YEAR}).")
        return
    set_profile_birth_year(user_id, int(text))
    clear_pending(user_id)
    if edit:
        bot.send_message(message.chat.id, "✅ Tug'ilgan yilingiz yangilandi.", reply_markup=main_menu_keyboard(user_id))
    else:
        mark_profile_complete(user_id)
        bot.send_message(message.chat.id, "Rahmat! Profilingiz to'ldi ✅", reply_markup=main_menu_keyboard(user_id))


def _process_target_id_input(message, user_id):
    text = message.text.strip()
    if not text.isdigit():
        bot.send_message(message.chat.id, "⚠️ Iltimos, faqat raqamlardan iborat ID kiriting.")
        return
    target_id = int(text)

    if target_id == user_id:
        bot.send_message(message.chat.id, "⚠️ O'zingizga xabar yubora olmaysiz. Boshqa ID kiriting.")
        return
    if not user_exists(target_id):
        bot.send_message(message.chat.id, "⚠️ Bu ID bo'yicha foydalanuvchi topilmadi. Qayta kiriting.")
        return
    if not is_admin(user_id) and is_blocked(target_id, user_id):
        bot.send_message(
            message.chat.id,
            "🚫 Kechirasiz, bu foydalanuvchi sizni bloklagan, xabar yubora olmaysiz.",
            reply_markup=main_menu_keyboard(user_id),
        )
        clear_pending(user_id)
        return

    set_pending(user_id, {"step": "awaiting_message_text", "target_id": target_id})
    bot.send_message(message.chat.id, "✏️ Endi xabar matnini yozing:")


def _process_id_message_text(message, user_id, state):
    target_id = state["target_id"]
    text = message.text
    sender_is_admin = is_admin(user_id)

    if not sender_is_admin:
        if is_blocked(target_id, user_id):
            bot.send_message(
                message.chat.id,
                "🚫 Kechirasiz, bu foydalanuvchi sizni bloklagan, xabar yetkazilmadi.",
                reply_markup=main_menu_keyboard(user_id),
            )
            clear_pending(user_id)
            return

    set_pending(user_id, {"step": "confirm_id_message", "target_id": target_id, "text": text})
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("✅ Yuborish", callback_data="idmsgconfirm"),
        types.InlineKeyboardButton("❌ Bekor qilish", callback_data="idmsgcancel"),
    )
    bot.send_message(
        message.chat.id,
        "Quyidagi xabar yuboriladi:\n\n" + text + "\n\nYubormoqchimisiz?",
        reply_markup=kb,
    )


def _send_confirmed_id_message(chat_id, user_id, target_id, text):
    sender_is_admin = is_admin(user_id)
    header = "📩 Sizga xabar keldi (Bosh admin tomonidan yuborildi):" if sender_is_admin else "📩 Sizga yangi anonim xabar keldi:"

    try:
        bot.send_message(
            target_id,
            f"{header}\n\n{text}",
            reply_markup=message_action_keyboard(user_id, "id", hide_block=sender_is_admin),
        )
    except Exception as e:
        if is_blocked_error(e):
            mark_user_inactive(target_id)
        bot.send_message(
            chat_id,
            "⚠️ Xabarni yetkazib bo'lmadi. Ehtimol, foydalanuvchi botni bloklagan.",
            reply_markup=main_menu_keyboard(user_id),
        )
        return

    bot.send_message(chat_id, "✅ Xabaringiz yuborildi.", reply_markup=main_menu_keyboard(user_id))


def _process_reply_text(message, user_id, state):
    target_id = state["target_id"]
    mode = state.get("mode", "id")
    text = message.text
    sender_is_admin = is_admin(user_id)

    if not sender_is_admin and is_blocked(target_id, user_id):
        bot.send_message(
            message.chat.id, "🚫 Bu foydalanuvchi sizni bloklagan, javob yetkazilmadi.", reply_markup=main_menu_keyboard(user_id)
        )
        clear_pending(user_id)
        return

    set_pending(user_id, {"step": "confirm_reply_message", "target_id": target_id, "mode": mode, "text": text})
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("✅ Yuborish", callback_data="replyconfirm"),
        types.InlineKeyboardButton("❌ Bekor qilish", callback_data="replycancel"),
    )
    bot.send_message(
        message.chat.id,
        "Quyidagi javob yuboriladi:\n\n" + text + "\n\nYubormoqchimisiz?",
        reply_markup=kb,
    )


def _send_confirmed_reply(chat_id, user_id, target_id, mode, text):
    sender_is_admin = is_admin(user_id)
    header = "📩 Sizga javob keldi (Bosh admin tomonidan):" if sender_is_admin else "📩 Sizga javob keldi:"
    try:
        bot.send_message(
            target_id, f"{header}\n\n{text}", reply_markup=message_action_keyboard(user_id, mode, hide_block=sender_is_admin)
        )
        bot.send_message(chat_id, "✅ Javobingiz yuborildi.", reply_markup=main_menu_keyboard(user_id))
    except Exception as e:
        if is_blocked_error(e):
            mark_user_inactive(target_id)
        bot.send_message(
            chat_id, "⚠️ Javobni yetkazib bo'lmadi. Ehtimol, foydalanuvchi botni bloklagan.", reply_markup=main_menu_keyboard(user_id)
        )


def _process_admin_add_channel(message, user_id):
    raw = message.text.strip()
    identifier = normalize_channel_identifier(raw)

    if identifier is None:
        bot.send_message(
            message.chat.id,
            "⚠️ Bu formatni tushunmadim. Iltimos, @username yoki https://t.me/username ko'rinishida yuboring.\n"
            "(Shaxsiy taklif havolalari — t.me/+xxxx — hozircha qo'llab-quvvatlanmaydi.)",
        )
        return  # pending saqlanadi, qayta urinib ko'rishi mumkin

    if not is_bot_admin_in_chat(identifier):
        bot.send_message(
            message.chat.id,
            f"⚠️ Bot {identifier} ichida hali admin emas (yoki kanal topilmadi).\n"
            "Avval botni o'sha kanal/guruhga ADMIN qilib qo'shing, so'ng shu linkni qayta yuboring.",
        )
        return  # pending saqlanadi

    try:
        chat = bot.get_chat(identifier)
        title = chat.title or identifier
    except Exception:
        title = identifier

    add_forced_channel(identifier, title, user_id)
    clear_pending(user_id)
    bot.send_message(message.chat.id, f"✅ '{title}' majburiy azolik ro'yxatiga qo'shildi.", reply_markup=admin_menu_keyboard())


def _process_admin_broadcast(message, user_id):
    text = message.text
    set_pending(user_id, {"step": "confirm_broadcast", "text": text})
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("✅ Yuborish", callback_data="bcastconfirm"),
        types.InlineKeyboardButton("❌ Bekor qilish", callback_data="bcastcancel"),
    )
    bot.send_message(
        message.chat.id,
        "Quyidagi xabar botga start bosgan BARCHA foydalanuvchilarga yuboriladi:\n\n" + text + "\n\nYubormoqchimisiz?",
        reply_markup=kb,
    )


def _send_confirmed_broadcast(chat_id, user_id, text):
    user_ids = get_all_active_user_ids()
    success = 0
    failed = 0
    for uid in user_ids:
        try:
            bot.send_message(uid, text)
            success += 1
        except Exception as e:
            failed += 1
            if is_blocked_error(e):
                mark_user_inactive(uid)
    bot.send_message(
        chat_id,
        f"📣 Ajoyib! Xabaringiz {success} ta foydalanuvchiga muvaffaqiyatli yetib bordi!"
        + (f" ({failed} taga yetib bormadi — botni bloklashgan)" if failed else ""),
        reply_markup=admin_menu_keyboard(),
    )


# =============================================================
# INLINE TUGMALAR — XABAR/JAVOB/TARQATISH TASDIQLASH
# =============================================================


@bot.callback_query_handler(func=lambda c: c.data == "idmsgconfirm")
def handle_idmsg_confirm(call):
    user_id = call.from_user.id
    state = get_pending(user_id)
    if not state or state.get("step") != "confirm_id_message":
        bot.answer_callback_query(call.id, "Bu so'rov endi amal qilmaydi.")
        return
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    clear_pending(user_id)
    _send_confirmed_id_message(call.message.chat.id, user_id, state["target_id"], state["text"])


@bot.callback_query_handler(func=lambda c: c.data == "idmsgcancel")
def handle_idmsg_cancel(call):
    user_id = call.from_user.id
    clear_pending(user_id)
    bot.answer_callback_query(call.id, "Bekor qilindi.")
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    bot.send_message(call.message.chat.id, "Bekor qilindi.", reply_markup=main_menu_keyboard(user_id))


@bot.callback_query_handler(func=lambda c: c.data == "replyconfirm")
def handle_reply_confirm(call):
    user_id = call.from_user.id
    state = get_pending(user_id)
    if not state or state.get("step") != "confirm_reply_message":
        bot.answer_callback_query(call.id, "Bu so'rov endi amal qilmaydi.")
        return
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    clear_pending(user_id)
    _send_confirmed_reply(call.message.chat.id, user_id, state["target_id"], state["mode"], state["text"])


@bot.callback_query_handler(func=lambda c: c.data == "replycancel")
def handle_reply_cancel(call):
    user_id = call.from_user.id
    clear_pending(user_id)
    bot.answer_callback_query(call.id, "Bekor qilindi.")
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    bot.send_message(call.message.chat.id, "Bekor qilindi.", reply_markup=main_menu_keyboard(user_id))


@bot.callback_query_handler(func=lambda c: c.data == "bcastconfirm")
def handle_bcast_confirm(call):
    user_id = call.from_user.id
    if not is_admin(user_id):
        bot.answer_callback_query(call.id, "Ruxsat yo'q.")
        return
    state = get_pending(user_id)
    if not state or state.get("step") != "confirm_broadcast":
        bot.answer_callback_query(call.id, "Bu so'rov endi amal qilmaydi.")
        return
    bot.answer_callback_query(call.id, "Yuborilmoqda...")
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    clear_pending(user_id)
    _send_confirmed_broadcast(call.message.chat.id, user_id, state["text"])


@bot.callback_query_handler(func=lambda c: c.data == "bcastcancel")
def handle_bcast_cancel(call):
    user_id = call.from_user.id
    clear_pending(user_id)
    bot.answer_callback_query(call.id, "Bekor qilindi.")
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    bot.send_message(call.message.chat.id, "Bekor qilindi.", reply_markup=admin_menu_keyboard())


# =============================================================
# INLINE TUGMALAR — PROFIL (jins tanlash / tahrirlash)
# =============================================================


@bot.callback_query_handler(func=lambda c: c.data.startswith("setupgender|"))
def handle_setup_gender(call):
    user_id = call.from_user.id
    gender = call.data.split("|")[1]
    set_profile_gender(user_id, gender)
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    ask_birth_year(call.message.chat.id, user_id)


@bot.callback_query_handler(func=lambda c: c.data == "editname")
def handle_edit_name(call):
    user_id = call.from_user.id
    set_pending(user_id, {"step": "edit_name"})
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "Yangi ismingizni yozing:", reply_markup=cancel_keyboard())


@bot.callback_query_handler(func=lambda c: c.data == "edityear")
def handle_edit_year(call):
    user_id = call.from_user.id
    set_pending(user_id, {"step": "edit_birth_year"})
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "Yangi tug'ilgan yilingizni kiriting:", reply_markup=cancel_keyboard())


@bot.callback_query_handler(func=lambda c: c.data == "editgender_ask")
def handle_edit_gender_ask(call):
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "Iltimos, quyidagilardan birini tanlang:", reply_markup=gender_inline_keyboard("editgender"))


@bot.callback_query_handler(func=lambda c: c.data.startswith("editgender|"))
def handle_edit_gender(call):
    user_id = call.from_user.id
    gender = call.data.split("|")[1]
    set_profile_gender(user_id, gender)
    bot.answer_callback_query(call.id, "✅ Jinsingiz yangilandi.")
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    bot.send_message(call.message.chat.id, "✅ Yangilandi.", reply_markup=main_menu_keyboard(user_id))


# =============================================================
# INLINE TUGMALAR — RANDOM TANISHUV OQIMI
# =============================================================


@bot.callback_query_handler(func=lambda c: c.data.startswith("rndconfirm|"))
def handle_random_confirm(call):
    match_id = int(call.data.split("|")[1])
    match = get_pending_match(match_id)
    user_id = call.from_user.id
    if not match or match["requester_id"] != user_id or match["status"] != "preview":
        bot.answer_callback_query(call.id, "Bu so'rov endi amal qilmaydi.")
        return

    target_id = match["target_id"]
    requester_row = get_user(user_id)
    update_match_status(match_id, "waiting_response")
    set_random_wait(user_id, target_id)

    show_identity = is_admin(user_id)
    card = profile_card_text(requester_row, show_identity=show_identity)
    intro = "🎲 Bosh admin tomonidan tanishuv so'rovi keldi:\n\n" if show_identity else "🎲 Sizga tanishuv taklifi keldi!\n\n"

    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("✅ Ha", callback_data=f"rndyes|{match_id}"),
        types.InlineKeyboardButton("❌ Yo'q", callback_data=f"rndno|{match_id}"),
    )
    try:
        bot.send_message(target_id, intro + card + "\n\nTanishishni xohlaysizmi?", reply_markup=kb, parse_mode="HTML")
        bot.answer_callback_query(call.id, "So'rovingiz yuborildi ✅")
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        bot.send_message(call.message.chat.id, "Javobini kutamiz ⏳", reply_markup=main_menu_keyboard(user_id))
    except Exception as e:
        if is_blocked_error(e):
            mark_user_inactive(target_id)
        bot.answer_callback_query(call.id, "So'rovni yuborib bo'lmadi.")
        update_match_status(match_id, "cancelled")
        clear_random_wait(user_id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("rndcancel|"))
def handle_random_cancel(call):
    match_id = int(call.data.split("|")[1])
    match = get_pending_match(match_id)
    if match:
        update_match_status(match_id, "cancelled")
    bot.answer_callback_query(call.id, "Bekor qilindi.")
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data.startswith("rndno|"))
def handle_random_no(call):
    match_id = int(call.data.split("|")[1])
    match = get_pending_match(match_id)
    user_id = call.from_user.id
    if not match or match["target_id"] != user_id:
        bot.answer_callback_query(call.id, "Bu so'rov endi amal qilmaydi.")
        return
    update_match_status(match_id, "declined")
    clear_random_wait(match["requester_id"])
    bot.answer_callback_query(call.id, "Bekor qilindi.")
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    try:
        bot.send_message(
            match["requester_id"],
            "😔 Afsuski, bu safar tanishuv amalga oshmadi. Istasangiz, qayta qidirib ko'rishingiz mumkin.",
        )
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data.startswith("rndyes|"))
def handle_random_yes(call):
    match_id = int(call.data.split("|")[1])
    match = get_pending_match(match_id)
    user_id = call.from_user.id
    if not match or match["target_id"] != user_id or match["status"] != "waiting_response":
        bot.answer_callback_query(call.id, "Bu so'rov endi amal qilmaydi.")
        return
    update_match_status(match_id, "confirm_pending")
    bot.answer_callback_query(call.id)

    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("✅ Tasdiqlayman", callback_data=f"rndfinal|{match_id}"),
        types.InlineKeyboardButton("❌ Bekor qilish", callback_data=f"rndfinalcancel|{match_id}"),
    )
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    bot.send_message(
        call.message.chat.id,
        "☑️ Tasdiqlasangiz, ism/jins/yosh, Telegram ID va (bor bo'lsa) username ma'lumotlaringiz unga yuboriladi, "
        "siz esa uning ma'lumotlarini olasiz.\n\nDavom etamizmi?",
        reply_markup=kb,
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("rndfinalcancel|"))
def handle_random_final_cancel(call):
    match_id = int(call.data.split("|")[1])
    match = get_pending_match(match_id)
    user_id = call.from_user.id
    if not match or match["target_id"] != user_id:
        bot.answer_callback_query(call.id, "Bu so'rov endi amal qilmaydi.")
        return
    update_match_status(match_id, "declined")
    clear_random_wait(match["requester_id"])
    bot.answer_callback_query(call.id, "Bekor qilindi.")
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    try:
        bot.send_message(match["requester_id"], "😔 Afsuski, bu safar tanishuv amalga oshmadi.")
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data.startswith("rndfinal|"))
def handle_random_final_confirm(call):
    match_id = int(call.data.split("|")[1])
    match = get_pending_match(match_id)
    user_id = call.from_user.id
    if not match or match["target_id"] != user_id or match["status"] != "confirm_pending":
        bot.answer_callback_query(call.id, "Bu so'rov endi amal qilmaydi.")
        return

    requester_id = match["requester_id"]
    requester_row = get_user(requester_id)
    target_row = get_user(user_id)

    update_match_status(match_id, "accepted")
    clear_random_wait(requester_id)
    bot.answer_callback_query(call.id, "🎉 Tabriklaymiz!")
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass

    bot.send_message(
        call.message.chat.id,
        "🎉 Ma'lumotlar almashindi! Mana suhbatdoshingiz:\n\n" + profile_card_text(requester_row, show_identity=True) +
        "\n\nEndi bemalol yozishishingiz mumkin 🙂",
        parse_mode="HTML",
    )
    try:
        bot.send_message(
            requester_id,
            "🎉 Suhbatdoshingiz tanishishga rozi bo'ldi! Mana ma'lumotlari:\n\n" +
            profile_card_text(target_row, show_identity=True) + "\n\nEndi bemalol yozishishingiz mumkin 🙂",
            parse_mode="HTML",
        )
    except Exception:
        pass


# =============================================================
# INLINE TUGMALAR — ID XABAR (Bloklash / Javob yozish / Blokdan chiqarish)
# =============================================================


@bot.callback_query_handler(func=lambda c: c.data.startswith("block|"))
def handle_block_callback(call):
    blocker_id = call.from_user.id
    sender_id = int(call.data.split("|")[1])

    if is_admin(sender_id):
        bot.answer_callback_query(call.id, "Bu foydalanuvchini bloklab bo'lmaydi.")
        return
    if blocker_id == sender_id:
        bot.answer_callback_query(call.id, "O'zingizni bloklay olmaysiz.")
        return

    anon_label = add_block(blocker_id, sender_id)
    if anon_label is None:
        bot.answer_callback_query(call.id, "Bu foydalanuvchi allaqachon bloklangan.")
    else:
        bot.answer_callback_query(call.id, "🚫 Foydalanuvchi bloklandi.")
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception:
            pass


@bot.callback_query_handler(func=lambda c: c.data.startswith("reply|"))
def handle_reply_callback(call):
    user_id = call.from_user.id
    _, sender_id, mode = call.data.split("|")
    sender_id = int(sender_id)
    set_pending(user_id, {"step": "awaiting_reply_text", "target_id": sender_id, "mode": mode})
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "✍️ Javobingizni yozing:", reply_markup=cancel_keyboard())


@bot.callback_query_handler(func=lambda c: c.data.startswith("unblock|"))
def handle_unblock_callback(call):
    user_id = call.from_user.id
    blocked_user_id = int(call.data.split("|")[1])
    remove_block(user_id, blocked_user_id)
    bot.answer_callback_query(call.id, "Blokdan chiqarildi.")

    kb = blocked_list_keyboard(user_id)
    try:
        if kb is None:
            bot.edit_message_text("🚫 Bloklangan foydalanuvchilar yo'q.", call.message.chat.id, call.message.message_id)
        else:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=kb)
    except Exception:
        pass


# =============================================================
# BOT QO'SHILGAN/CHIQARILGAN GURUH-KANALLARNI KUZATISH (xabar tarqatish uchun)
# =============================================================


@bot.my_chat_member_handler()
def handle_my_chat_member(update):
    chat = update.chat
    new_status = update.new_chat_member.status
    if new_status in ("member", "administrator"):
        upsert_broadcast_chat(chat.id, chat.title or str(chat.id), chat.type)
    elif new_status in ("left", "kicked"):
        remove_broadcast_chat(chat.id)


# =============================================================
# ISHGA TUSHIRISH
# =============================================================

if __name__ == "__main__":
    init_db()
    BOT_ID = bot.get_me().id
    print("Bot ishga tushdi...")
    bot.infinity_polling(skip_pending=True)