from telethon import TelegramClient
from config import API_ID, API_HASH, SESSION_NAME

print("=== Авторизация Telethon ===")
client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

# client.start() сам запросит номер телефона, код из Telegram и пароль 2FA (если есть)
client.start()

print("\n Авторизация успешно завершена! Файл сессии создан.")
client.disconnect()