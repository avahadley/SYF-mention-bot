import os
import asyncio
import logging
import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, ChatMemberUpdated
from aiogram.enums import ChatType
from aiogram.client.default import DefaultBotProperties

# ================= Config & logging =================
TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    raise SystemExit("Missing TELEGRAM_TOKEN environment variable")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))  # aiogram 3.7+ syntax
dp = Dispatcher()

dp = Dispatcher()

# --- DEBUG: log & quick test in groups ---
@dp.message(Command("ping"))
async def ping(msg: Message):
    await msg.reply("pong ✅")

@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def _debug_logger(msg: Message):
    who = (msg.from_user.username or (msg.from_user.full_name if msg.from_user else "unknown"))
    logging.info(f"DBG group {msg.chat.id} from {who}: {msg.text!r}")
    if msg.text and msg.text.startswith("/debug"):
        await msg.reply(f"Seen debug in group {msg.chat.id}.")

DB_PATH = "data.db"

DEFAULTS = {
    "only_admins": True,
    "copy_message": False,
    "tag_style": "empty",  # empty | emoji | name
    "emoji": "🔔",
    "chunk_size": 8,
    "delay_ms": 900,
}

# Global DB connection + lock to avoid aiosqlite thread errors
DB: aiosqlite.Connection | None = None
DB_LOCK = asyncio.Lock()

CREATE_SQL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS chat_config (
  chat_id INTEGER PRIMARY KEY,
  only_admins INTEGER NOT NULL DEFAULT 1,
  copy_message INTEGER NOT NULL DEFAULT 0,
  tag_style TEXT NOT NULL DEFAULT 'empty',
  emoji TEXT NOT NULL DEFAULT '🔔',
  chunk_size INTEGER NOT NULL DEFAULT 8,
  delay_ms INTEGER NOT NULL DEFAULT 900
);
CREATE TABLE IF NOT EXISTS members (
  chat_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  first_name TEXT,
  last_name TEXT,
  username TEXT,
  PRIMARY KEY (chat_id, user_id)
);
"""

# ================= DB helpers =================
async def init_db():
    """Create one global connection and initialize tables."""
    global DB
    if DB is None:
        DB = await aiosqlite.connect(DB_PATH)
        DB.row_factory = aiosqlite.Row
        await DB.executescript(CREATE_SQL)
        await DB.commit()
    logging.info("DB ready")

async def get_config(chat_id: int):
    async with DB_LOCK:
        cur = await DB.execute("SELECT * FROM chat_config WHERE chat_id=?", (chat_id,))
        row = await cur.fetchone()
        if not row:
            await DB.execute(
                """INSERT INTO chat_config
                   (chat_id, only_admins, copy_message, tag_style, emoji, chunk_size, delay_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (chat_id, int(DEFAULTS["only_admins"]), int(DEFAULTS["copy_message"]),
                 DEFAULTS["tag_style"], DEFAULTS["emoji"], DEFAULTS["chunk_size"], DEFAULTS["delay_ms"])
            )
            await DB.commit()
            return DEFAULTS.copy()
        return {
            "only_admins": bool(row["only_admins"]),
            "copy_message": bool(row["copy_message"]),
            "tag_style": row["tag_style"],
            "emoji": row["emoji"],
            "chunk_size": row["chunk_size"],
            "delay_ms": row["delay_ms"],
        }

async def set_config(chat_id: int, **kwargs):
    cfg = await get_config(chat_id)
    cfg.update(kwargs)
    async with DB_LOCK:
        await DB.execute(
            """REPLACE INTO chat_config(chat_id, only_admins, copy_message, tag_style, emoji, chunk_size, delay_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (chat_id, int(cfg["only_admins"]), int(cfg["copy_message"]), cfg["tag_style"],
             cfg["emoji"], cfg["chunk_size"], cfg["delay_ms"])
        )
        await DB.commit()
    return cfg

async def upsert_member(chat_id: int, user):
    async with DB_LOCK:
        await DB.execute(
            """REPLACE INTO members(chat_id, user_id, first_name, last_name, username)
               VALUES (?, ?, ?, ?, ?)""",
            (chat_id, user.id, user.first_name or "", user.last_name or "", user.username or "")
        )
        await DB.commit()

async def delete_member(chat_id: int, user_id: int):
    async with DB_LOCK:
        await DB.execute("DELETE FROM members WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        await DB.commit()

async def list_members(chat_id: int):
    async with DB_LOCK:
        cur = await DB.execute("SELECT * FROM members WHERE chat_id=?", (chat_id,))
        rows = await cur.fetchall()
        return rows

# ================= Utils =================
stop_flags: dict[int, asyncio.Event] = {}

async def is_admin(chat_id: int, user_id: int) -> bool:
    try:
        admins = await bot.get_chat_administrators(chat_id)
        return any(a.user.id == user_id for a in admins)
    except Exception:
        return False

def build_mention_text(row, style: str, emoji: str):
    uid = row["user_id"]
    full_name = " ".join([n for n in [row["first_name"], row["last_name"]] if n]).strip() or "member"
    mention = f'<a href="tg://user?id={uid}">\u2063</a>'  # invisible mention keeps ping
    visible_handle = f'@{row["username"]}' if row["username"] else ""
    if style == "empty":
        return mention if not visible_handle else f'{visible_handle}{mention}'
    if style == "emoji":
        return f'{emoji} {visible_handle or full_name}{mention}'
    return f'{full_name}{mention}'

def chunkify(seq, n):
    buf = []
    for x in seq:
        buf.append(x)
        if len(buf) >= n:
            yield buf
            buf = []
    if buf:
        yield buf

def get_stop_flag(chat_id: int) -> asyncio.Event:
    flag = stop_flags.get(chat_id)
    if not flag:
        flag = asyncio.Event()
        stop_flags[chat_id] = flag
    return flag

# ================= Learn members =================
@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def learn_active_users(msg: Message):
    if not msg.from_user or msg.from_user.is_bot:
        return
    await upsert_member(msg.chat.id, msg.from_user)

@dp.chat_member()
async def member_updates(ev: ChatMemberUpdated):
    chat_id = ev.chat.id
    new = ev.new_chat_member
    user = new.user
    if new.status in {"member", "administrator", "creator"}:
        await upsert_member(chat_id, user)
    if new.status in {"left", "kicked"}:
        await delete_member(chat_id, user.id)

# ================= Commands =================
@dp.message(Command("start"))
async def start_dm(msg: Message):
    await msg.answer("✅ Bot is online.\nAdd me to a group, make me admin, then try /all.")

@dp.message(Command("onlyadmins"))
async def onlyadmins(msg: Message):
    await set_config(msg.chat.id, only_admins=True)
    await msg.reply("✅ `/all` is now admins only.", parse_mode="Markdown")

@dp.message(Command("noonlyadmins"))
async def noonlyadmins(msg: Message):
    await set_config(msg.chat.id, only_admins=False)
    await msg.reply("✅ `/all` allowed for everyone.", parse_mode="Markdown")

@dp.message(Command("copymessage"))
async def copymessage(msg: Message):
    await set_config(msg.chat.id, copy_message=True)
    await msg.reply("✅ Tagging will copy the replied message (if present).")

@dp.message(Command("nocopymessage"))
async def nocopymessage(msg: Message):
    await set_config(msg.chat.id, copy_message=False)
    await msg.reply("✅ Tagging will send fresh messages.")

@dp.message(Command("emptytagtype"))
async def emptytagtype(msg: Message):
    await set_config(msg.chat.id, tag_style="empty")
    await msg.reply("✅ Tag style set to plain mentions.")

@dp.message(Command("emojitagtype"))
async def emojitagtype(msg: Message):
    cfg = await get_config(msg.chat.id)
    await set_config(msg.chat.id, tag_style="emoji", emoji=cfg["emoji"])
    await msg.reply(f"✅ Tag style set to emoji ({cfg['emoji']}).")

@dp.message(Command("nametagtype"))
async def nametagtype(msg: Message):
    await set_config(msg.chat.id, tag_style="name")
    await msg.reply("✅ Tag style set to Name + mention.")

@dp.message(Command("stopall"))
async def stopall(msg: Message):
    get_stop_flag(msg.chat.id).set()
    await msg.reply("🛑 Stopping current tag run (if any).")

@dp.message(Command("all"))
async def tag_all(msg: Message):
    chat_id = msg.chat.id
    user_id = msg.from_user.id if msg.from_user else 0
    cfg = await get_config(chat_id)

    if cfg["only_admins"] and not await is_admin(chat_id, user_id):
        await msg.reply("⛔ Only admins can use `/all` here.", parse_mode="Markdown")
        return

    rows = await list_members(chat_id)
    if not rows:
        await msg.reply("I don’t know anyone here yet. Send a few messages so I can learn members.")
        return

    flag = get_stop_flag(chat_id)
    flag.clear()

    mentions = [build_mention_text(r, cfg["tag_style"], cfg["emoji"]) for r in rows]
    chunks = list(chunkify(mentions, max(1, int(cfg["chunk_size"]))))

    header = "📣 Tagging everyone…" if cfg["tag_style"] != "emoji" else f"{cfg['emoji']} Tagging everyone…"
    await msg.reply(f"{header} ({len(rows)} members known)")

    replied = msg.reply_to_message if cfg["copy_message"] and msg.reply_to_message else None

    for chunk in chunks:
        if flag.is_set():
            await msg.answer("✅ Stopped.")
            break
        text = " ".join(chunk)
        if replied:
            try:
                sent = await replied.copy_to(chat_id, reply_to_message_id=None)
                await bot.send_message(chat_id, text, reply_to_message_id=sent.message_id, disable_web_page_preview=True)
            except Exception:
                await bot.send_message(chat_id, text, disable_web_page_preview=True)
        else:
            await bot.send_message(chat_id, text, disable_web_page_preview=True)
        await asyncio.sleep(cfg["delay_ms"] / 1000)

    if not flag.is_set():
        await msg.answer("✅ Done.")

# ================= Runner =================
async def main():
    await init_db()
    logging.info("Starting polling…")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
