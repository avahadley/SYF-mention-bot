import asyncio
import logging
import os
import aiosqlite
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

# Enable logging
logging.basicConfig(level=logging.INFO)

# Initialize bot and dispatcher
bot = Bot(token=TOKEN)
dp = Dispatcher()

DB_PATH = "mentions.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS mentions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            message TEXT
        )
        """)
        await db.commit()

@dp.message(Command("start"))
async def start_command(message: types.Message):
    await message.answer("Hello! I will save mentions for you.")

@dp.message()
async def save_mentions(message: types.Message):
    if message.entities:
        for entity in message.entities:
            if entity.type == "mention":
                mention_text = message.text[entity.offset:entity.offset + entity.length]
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "INSERT INTO mentions (user_id, username, message) VALUES (?, ?, ?)",
                        (message.from_user.id, message.from_user.username, mention_text)
                    )
                    await db.commit()
                await message.answer(f"Saved mention: {mention_text}")

async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
