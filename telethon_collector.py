import asyncio
import os
from telethon import TelegramClient
from telethon.tl.types import Channel, PeerChannel

import aiosqlite

from config import API_ID, API_HASH, SESSION_NAME, DB_NAME, MEDIA_DIR

def format_channel_peer(channel_id: int):
    """Преобразует ID канала в корректный PeerChannel для Telethon."""
    # Если передан -100123..., достаем чистый ID
    s_id = str(channel_id)
    if s_id.startswith("-100"):
        clean_id = int(s_id[4:])
    elif s_id.startswith("-"):
        clean_id = int(s_id[1:])
    else:
        clean_id = int(s_id)
    return PeerChannel(clean_id)


async def sync_user_channels(client: TelegramClient):
    """Синхронизирует каналы. Сохраняет корректный dialog.id с префиксом -100."""
    print(" Сканирование каналов аккаунта...")
    found = 0
    async with aiosqlite.connect(DB_NAME) as db:
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            if isinstance(entity, Channel) and entity.broadcast:
                is_private = entity.username is None
                # dialog.id всегда содержит правильный префикс -100 для каналов
                c_id = dialog.id

                await db.execute("""
                    INSERT INTO channels (channel_id, title, username, is_private, is_active)
                    VALUES (?, ?, ?, ?, 0)
                    ON CONFLICT(channel_id) DO UPDATE SET
                        title = excluded.title,
                        username = excluded.username,
                        is_private = excluded.is_private
                """, (c_id, dialog.title, getattr(entity, "username", None), is_private))
                found += 1
        await db.commit()
    print(f" Синхронизировано каналов: {found}")


async def get_active_channels():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT channel_id, title FROM channels WHERE is_active = 1") as cursor:
            return await cursor.fetchall()


async def download_photo_safe(client: TelegramClient, msg, dest_path: str) -> str | None:
    """Безопасное скачивание без сбоев сессии."""
    if not msg.photo:
        return None
    if os.path.exists(dest_path):
        return dest_path

    try:
        downloaded = await asyncio.wait_for(
            client.download_media(msg.photo, file=dest_path, thumb=-1),
            timeout=7.0
        )
        return downloaded
    except Exception:
        # При любых сбоях (сеть, таймаут, CDN) пропускаем медиа, пост сохранится текстом
        return None


async def collect_posts_from_channels(limit_per_channel: int = 10):
    channels = await get_active_channels()
    if not channels:
        print("⚠️ Нет активных каналов для сбора! Включите каналы в меню 'Список каналов'.")
        return 0

    print(f" Сбор постов из {len(channels)} выбранных каналов...")
    total_new = 0

    # Ограничиваем попытки реконнекта, чтобы не уходить в бесконечный цикл
    client = TelegramClient(
        SESSION_NAME,
        API_ID,
        API_HASH,
        connection_retries=3,
        retry_delay=2,
        auto_reconnect=True
    )

    async with client:
        async with aiosqlite.connect(DB_NAME) as db:
            for channel_id, title in channels:
                try:
                    peer = format_channel_peer(channel_id)
                    entity = await client.get_entity(peer)
                    added_in_channel = 0

                    async for msg in client.iter_messages(entity, limit=limit_per_channel):
                        text = msg.text or msg.message or ""
                        if len(text.strip()) < 30 and not msg.photo:
                            continue

                        dest_file = str(MEDIA_DIR / f"{entity.id}_{msg.id}.jpg")
                        media_path = await download_photo_safe(client, msg, dest_file)

                        cursor = await db.execute("""
                            INSERT OR IGNORE INTO messages 
                            (channel_id, message_id, channel_title, text, media_path, is_processed, is_summarized)
                            VALUES (?, ?, ?, ?, ?, 0, 0)
                        """, (entity.id, msg.id, title, text.strip(), media_path))

                        if cursor.rowcount > 0:
                            added_in_channel += 1

                    await db.commit()
                    if added_in_channel > 0:
                        print(f"  [+] {title}: +{added_in_channel} постов")
                        total_new += added_in_channel

                    # Небольшая пауза для защиты от лимитов Telegram
                    await asyncio.sleep(0.5)

                except Exception as e:
                    print(f"  [!] Пропуск канала '{title}': {e}")

    print(f" Всего добавлено новых постов: {total_new}")
    return total_new