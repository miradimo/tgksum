import os
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(".env")
# Telegram API (публичные ключи Telegram Desktop)
ADMIN_ID = int(os.getenv("ADMIN_ID"))
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_NAME = "tg_user_session"

# Telegram Bot (для публикации)
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = int(os.getenv("CHAT_ID"))  # ID супергруппы

# Привязка топиков (thread_id)
TOPIC_THREADS = {
    "svo_military": 2,
    "geopolitics": 4,
    "tech_ai": 6,
    "finance": 8,
    "other": 10,
}

# Настройки базы и директорий
DB_NAME = "news.db"
MEDIA_DIR = Path("downloads")
MEDIA_DIR.mkdir(exist_ok=True)

# Модели Ollama
MODEL_CLASSIFIER = "qwen2.5:7b"
MODEL_SUMMARIZER = "qwen2.5:14b"

# Qdrant settings
QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", 6333))
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "telegram_posts")
EMBEDDING_DIM = 312  # Для cointegrated/rubert-tiny2