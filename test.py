import asyncio
import aiosqlite

DB_NAME = "news.db"


async def reset_db():
  async with aiosqlite.connect(DB_NAME) as db:
    await db.execute("DELETE FROM messages")
    await db.execute("DELETE FROM sqlite_sequence WHERE name='messages'")
    await db.commit()
  print("База данных успешно очищена от моковых записей.")

import aiosqlite, asyncio

async def add_col():
    async with aiosqlite.connect("news.db") as db:
        try:
            await db.execute("ALTER TABLE messages ADD COLUMN media_url TEXT DEFAULT NULL")
            await db.commit()
        except:
            pass # колонка уже существует
asyncio.run(add_col())


if __name__ == "__main__":
  asyncio.run(add_col())