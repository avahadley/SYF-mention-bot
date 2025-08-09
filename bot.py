# bot.py
import os
import asyncio
import logging
import aiosqlite

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, ChatMemberUpdated
from aiogram.enums import ChatType
from aiogram.client.default import DefaultBotProperties

# -------------------- config & logging --------------------
TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    raise SystemExit("Missing TELEGRAM_TOKEN environment variable")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))  # aiogram 3.7+
dp = Dispatcher()

DB_PATH = "data.db"
DB: aiosqlite.Connection | None = None
DB_LOCK = asyncio.Lock()

DEFAULTS = {
    "only_admins": True,     # /onlyadmins or /noonlyadmins
    "copy_message": False,   # /copymessage or /nocopymessage
    "tag_style": "empty",    # empty | emoji | name   (/emptytagtype /emojitagtype /nametagtype)
    "emoji": "🔔",
    "chunk_size": 8,
    "delay_ms": 900,
}

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

# -------------------- db helpers --------------------
async def init_db():
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
                (chat_id, 1 if DEFAULTS["only_admins"] else 0,
                 1 if DEFAULTS["copy_message"] else 0,
                 DEFAULTS["tag_style"], DEFAULTS["emoji"],
                 DEFAULTS["chunk_size"], DEFAULTS["delay_ms"])
            )
            await DB.commit()
            return DEFAULTS.copy()
        return {
            "only_admins": bool(row["only_admins"]),
            "copy_message": bool(row["copy_message"]),
            "tag_style": row["tag_style"],
            "emoji": row["emoji"],
            "chunk_size": int(row["chunk_size"]),
            "delay_ms": int(row["delay_ms"]),
        }

async def set_config(chat_id: int, **kwargs):
    cfg = await get_config(chat_id)
    cfg.update(kwargs)
    async with DB_LOCK:
        await DB.execute(
            """REPLACE INTO chat_config(chat_id, only_admins, copy_message, tag_style, emoji, chunk_size, delay_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (chat_id,
             1 if cfg["only_admins"] else 0,
             1 if cfg["copy_message"] else 0,
             cfg["tag_style"], cfg["emoji"], int(cfg["chunk_size"]), int(cfg["delay_ms"]))
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
        return await cur.fetchall()

# -------------------- utils --------------------
stop_flags: dict[int, asyncio.Event] = {}

async def is_admin(chat_id: int, user_id: int) -> bool:
    try:
        for adm in await bot.get_chat_administrators(chat_id):
            if adm.user.id == user_id:
                return True
    except Exception:
        pass
    return False

def build_mention_text(row, style: str, emoji: str):
    uid = row["user_id"]
    full_name = " ".join([n for n in [row["first_name"], row["last_name"]] if n]).strip() or "member"
    # invisible clickable mention to trigger notification
    invisible = f'<a href="tg://user?id={uid}">\u2063</a>'
    handle = f'@{row["username"]}' if row["username"] else ""
    if style == "empty":
        return (handle or "") + invisible
    if style == "emoji":
        return f'{emoji} {handle or full_name}{invisible}'
    return f'{full_name}{invisible}'

def chunkify(items, n):
    buf = []
    for x in items:
        buf.append(x)
        if len(buf) >= n:
            yield buf
            buf = []
    if buf:
        yield buf

def stop_flag(chat_id: int) -> asyncio.Event:
    flag = stop_flags.get(chat_id)
    if not flag:
        flag = asyncio.Event()
        stop_flags[chat_id] = flag
    return flag

# -------------------- commands (placed first!) --------------------
@dp.message(Command("start"))
async def cmd_start(msg: Message):
    await msg.answer("✅ Bot is online.\nAdd me to a group, make me admin, then try /all.")

@dp.message(Command("ping"))
async def cmd_ping(msg: Message):
    await msg.reply("pong ✅")

@dp.message(Command("onlyadmins"))
async def cmd_onlyadmins(msg: Message):
    await set_config(msg.chat.id, only_admins=True)
    await msg.reply("✅ `/all` is now admins only.", parse_mode="Markdown")

@dp.message(Command("noonlyadmins"))
async def cmd_noonlyadmins(msg: Message):
    await set_config(msg.chat.id, only_admins=False)
    await msg.reply("✅ `/all` allowed for everyone.", parse_mode="Markdown")

@dp.message(Command("copymessage"))
async def cmd_copymessage(msg: Message):
    await set_config(msg.chat.id, copy_message=True)
    await msg.reply("✅ Tagging will copy the replied message (if present).")

@dp.message(Command("nocopymessage"))
async def cmd_nocopymessage(msg: Message):
    await set_config(msg.chat.id, copy_message=False)
    await msg.reply("✅ Tagging will send fresh messages.")

@dp.message(Command("emptytagtype"))
async def cmd_empty(msg: Message):
    await set_config(msg.chat.id, tag_style="empty")
    await msg.reply("✅ Tag style set to plain mentions.")

@dp.message(Command("emojitagtype"))
async def cmd_emoji(msg: Message):
    cfg = await get_config(msg.chat.id)
    await set_config(msg.chat.id, tag_style="emoji", emoji=cfg["emoji"])
    await msg.reply(f"✅ Tag style set to emoji ({cfg['emoji']}).")

@dp.message(Command("nametagtype"))
async def cmd_name(msg: Message):
    await set_config(msg.chat.id, tag_style="name")
    await msg.reply("✅ Tag style set to Name + mention.")

@dp.message(Command("stopall"))
async def cmd_stopall(msg: Message):
    stop_flag(msg.chat.id).set()
    await msg.reply("🛑 Stopping current tag run (if any).")

@dp.message(Command("all"))
async def cmd_all(msg: Message):
    chat_id = msg.chat.id
    user_id = msg.from_user.id if msg.from_user else 0
    cfg = await get_config(chat_id)

    # admin check
    if cfg["only_admins"] and not await is_admin(chat_id, user_id):
        await msg.reply("⛔ Only admins can use `/all` here.", parse_mode="Markdown")
        return

    rows = await list_members(chat_id)
    if not rows:
        await msg.reply("I don’t know anyone here yet. Send a few messages so I can learn members.")
        return

    flag = stop_flag(chat_id)
    flag.clear()

    mentions = [build_mention_text(r, cfg["tag_style"], cfg["emoji"]) for r in rows]
    chunks = list(chunkify(mentions, max(1, int(cfg["chunk_size"]))))

    header = "📣 Tagging everyone…" if cfg["tag_style"] != "emoji" else f"{cfg['emoji']} Tagging everyone…"
    await msg.reply(f"{header} ({len(rows)} members known)")

    replied = msg.reply_to_message if (cfg["copy_message"] and msg.reply_to_message) else None

    for chunk in chunks:
        if flag.is_set():
            await msg.answer("✅ Stopped.")
            break
        text = " ".join(chunk)
        if replied:
            try:
                sent = await replied.copy_to(chat_id)
                await bot.send_message(chat_id, text, reply_to_message_id=sent.message_id, disable_web_page_preview=True)
            except Exception:
                await bot.send_message(chat_id, text, disable_web_page_preview=True)
        else:
            await bot.send_message(chat_id, text, disable_web_page_preview=True)
        await asyncio.sleep(cfg["delay_ms"] / 1000)

    if not flag.is_set():
        await msg.answer("✅ Done.")

# -------------------- learn members (after commands!) --------------------
# Ignore messages that start with "/" so commands are not swallowed
CMD_FILTER = ~F.text.regexp(r"^/\w+")

@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}) & CMD_FILTER)
async def learn_active_users(msg: Message):
    if msg.from_user and not msg.from_user.is_bot:
        await upsert_member(msg.chat.id, msg.from_user)

@dp.chat_member()
async def member_updates(ev: ChatMemberUpdated):
    chat_id = ev.chat.id
    u = ev.new_chat_member.user
    st = ev.new_chat_member.status
    if st in {"member", "administrator", "creator"}:
        await upsert_member(chat_id, u)
    elif st in {"left", "kicked"}:
        await delete_member(chat_id, u.id)

# -------------------- runner --------------------
async def main():
    await init_db()
    logging.info("Starting polling…")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
