import sys
import asyncio
from aiogram import Bot
import aiosqlite

from config import BOT_TOKEN, CHAT_ID, TOPIC_THREADS
import config
from database import init_db
from telethon_collector import collect_posts_from_channels, sync_user_channels
from classifier import classify_unprocessed
from summarizer import generate_all_cards
from dynamic_topics import process_other_news
from publisher import publish_cards_to_topic, publish_dynamic_digest
from vector_store import VectorStore
import os
import logging

# 1. Для Windows переключаем event loop на Selector (убирает Task destroyed и GeneratorExit)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# 2. Глушим шумные внутренние варнинги Telethon
logging.getLogger("telethon").setLevel(logging.ERROR)

# 3. Отключаем предупреждение от Hugging Face
os.environ["TOKENIZERS_PARALLELISM"] = "false"

def build_message_link(channel_username: str, channel_id: int, message_id: int) -> str:
    if channel_username:
        clean_user = channel_username.lstrip("@")
        return f"https://t.me/{clean_user}/{message_id}"
    if channel_id:
        clean_id = str(channel_id).replace("-100", "").lstrip("-")
        return f"https://t.me/c/{clean_id}/{message_id}"
    return ""


async def index_new_posts(db_path: str):
    """Индексирует свежесобранные посты в Qdrant."""
    store = VectorStore()
    await store.init_collection()

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        # Забираем обработанные классификатором посты
        query = """
            SELECT 
                m.id, m.text, m.channel_title, m.topic, m.created_at,
                m.message_id, m.channel_id, c.username AS channel_username
            FROM messages m
            LEFT JOIN channels c ON m.channel_id = c.channel_id
            WHERE m.is_processed = 1 AND m.text IS NOT NULL AND length(trim(m.text)) > 20
        """
        async with db.execute(query) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        return

    posts = []
    for row in rows:
        p = dict(row)
        p["message_link"] = build_message_link(
            p.get("channel_username"), p.get("channel_id"), p.get("message_id")
        )
        posts.append(p)

    # Qdrant upsert перезаписывает точки с теми же ID, дублей не будет
    await store.upsert_posts(posts)
    print(f"Векторный индекс обновлен: проверено/обновлено {len(posts)} постов.")

async def run_pipeline():
    print("=== [1/5] Инициализация БД ===")
    await init_db()

    print("\n=== [2/5] Сбор постов и медиа через Telethon ===")
    await collect_posts_from_channels(limit_per_channel=10)

    print("\n=== [3/5] Асинхронная классификация по темам (Ollama) ===")
    await classify_unprocessed(concurrency=4)
    await index_new_posts(config.DB_NAME)
    print("\n=== [4/5] Кластеризация инфоповодов и суммаризация ===")
    # Вся кластеризация через EventClusterer происходит внутри generate_all_cards()
    all_cards = await generate_all_cards()
    other_digest = await process_other_news()

    total_cards = sum(len(cards) for cards in all_cards.values())
    print(f"\nСгенерировано готовых карточек: {total_cards}")

    print("\n=== [5/5] Отправка карточек в топики Telegram ===")
    bot = Bot(token=BOT_TOKEN)
    try:
        # 1. Отправляем основные темы
        for topic, cards in all_cards.items():
            if not cards:
                continue

            thread_id = TOPIC_THREADS.get(topic)
            if not thread_id:
                print(f"Предупреждение: для темы [{topic}] не настроен thread_id в config.py")
                continue

            print(f"Отправка {len(cards)} карточек в топик [{topic}] (thread {thread_id})...")
            await publish_cards_to_topic(bot, CHAT_ID, thread_id, cards)

        # 2. Отправляем блок 'Other' (если есть)
        if other_digest:
            other_thread = TOPIC_THREADS.get("other")
            if other_thread:
                print(f"Отправка дайджеста 'Other' в топик {other_thread}...")
                await publish_dynamic_digest(bot, CHAT_ID, other_thread, other_digest)
            else:
                print("Предупреждение: thread_id для 'other' не настроен")
    except Exception as e:
        print(f"Ошибка при публикации в Telegram: {e}")
    finally:
        await bot.session.close()

    print("\n✅ Полный цикл обработки и публикации успешно завершен!")


if __name__ == "__main__":
    # Запуск синхронизации каналов: python run_pipeline.py --sync
    if len(sys.argv) > 1 and sys.argv[1] == "--sync":
        from telethon import TelegramClient
        from config import API_ID, API_HASH, SESSION_NAME

        async def do_sync():
            await init_db()
            async with TelegramClient(SESSION_NAME, API_ID, API_HASH) as client:
                await sync_user_channels(client)

        asyncio.run(do_sync())
    else:
        asyncio.run(run_pipeline())