import sys
import asyncio
from aiogram import Bot

from config import BOT_TOKEN, CHAT_ID, TOPIC_THREADS
from database import init_db
from telethon_collector import collect_posts_from_channels, sync_user_channels
from classifier import classify_unprocessed
from summarizer import generate_all_cards
from dynamic_topics import process_other_news
from publisher import publish_cards_to_topic, publish_dynamic_digest

async def run_pipeline():
    print("=== [1/5] Инициализация БД ===")
    await init_db()

    print("\n=== [2/5] Сбор постов и медиа через Telethon ===")
    await collect_posts_from_channels(limit_per_channel=10)

    print("\n=== [3/5] Асинхронная классификация через Ollama ===")
    await classify_unprocessed(concurrency=4)

    print("\n=== [4/5] Суммаризация и подготовка карточек ===")
    all_cards = await generate_all_cards()
    other_digest = await process_other_news()

    print("\n=== [5/5] Отправка в топики Telegram ===")
    bot = Bot(token=BOT_TOKEN)
    try:
        # 1. Отправляем основные темы
        for topic, cards in all_cards.items():
            thread_id = TOPIC_THREADS.get(topic)
            if not thread_id:
                continue
            print(f"Отправка {len(cards)} карточек в топик [{topic}] (thread {thread_id})...")
            await publish_cards_to_topic(bot, CHAT_ID, thread_id, cards)

        # 2. Отправляем Other
        if other_digest:
            other_thread = TOPIC_THREADS.get("other")
            if other_thread:
                print(f"Отправка дайджеста 'Other' в топик {other_thread}...")
                await publish_dynamic_digest(bot, CHAT_ID, other_thread, other_digest)
    finally:
        await bot.session.close()

    print("\n Полный цикл обработки успешно завершен!")

if __name__ == "__main__":
    # Если запустить с аргументом --sync: python run_pipeline.py --sync
    if len(sys.argv) > 1 and sys.argv == "--sync":
        from telethon import TelegramClient
        from config import API_ID, API_HASH, SESSION_NAME
        async def do_sync():
            await init_db()
            async with TelegramClient(SESSION_NAME, API_ID, API_HASH) as client:
                await sync_user_channels(client)
        asyncio.run(do_sync())
    else:
        asyncio.run(run_pipeline())