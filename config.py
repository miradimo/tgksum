import os
from pathlib import Path

# Telegram API (публичные ключи Telegram Desktop)
ADMIN_ID = 711318539
API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"
SESSION_NAME = "tg_user_session"

# Telegram Bot (для публикации)
BOT_TOKEN = "8818750275:AAF0ykp0DyOtHfhNkC47opQTkhNgcB0F384"
CHAT_ID = -1003968014145  # ID супергруппы

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
