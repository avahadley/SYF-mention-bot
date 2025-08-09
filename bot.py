import asyncio
import logging
import os
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message

# ========= Config =========
# Read the token from environment (Render: Environment Variables)
TOKEN = os.getenv("TELEGRAM_TOKEN")

# Fail fast if token is missing
if not TOKEN:
    raise SystemExit("Missing TELEGRAM_TOKEN environment variable")

# Logging helps us debug in Render logs
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Aiogram v3 setup
bot = Bot(token=TOKEN, parse_mode="HTML")
dp = Dispatcher()

# ===== Handlers =====
@dp.message(CommandStart())
async def on_start(m: Message):
    await m.answer("✅ Bot is online. Add me to a group and make me admin.\nTry /ping here or in a DM.")

@dp.message()
async def echo(m: Message):
    # Simple sanity check that the bot is responding
    if m.text and m.text.strip().lower() == "/ping":
        await m.answer("pong")
    elif m.text:
        await m.answer(f"You said: {m.text}")

# ===== Runner =====
async def main():
    logging.info("Starting polling…")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
