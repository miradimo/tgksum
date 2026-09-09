import os
import asyncio
from aiogram import Bot
from aiogram.types import FSInputFile

from config import BOT_TOKEN, CHAT_ID, TOPIC_THREADS

def truncate_caption(text: str, max_length: int = 1000) -> str:
    """Обрезает текст до лимита подписи к медиа в Telegram (1024 символа)."""
    if len(text) <= max_length:
        return text
    return text[:max_length - 3] + "..."

async def publish_cards_to_topic(bot: Bot, chat_id: int, thread_id: int, cards: list[dict]):
    for card in cards:
        body = f"🔹 **{card['headline']}**\n\n{card['text']}"
        if card.get("quote"):
            body += f"\n\n> {card['quote']}"

        media_path = card.get("media_path")
        # Проверяем, что media_path - это именно строка пути и файл реально есть на диске
        has_media = isinstance(media_path, str) and os.path.exists(media_path)

        try:
            if has_media:
                caption = truncate_caption(body)
                await bot.send_photo(
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                    photo=FSInputFile(media_path),
                    caption=caption,
                    parse_mode="Markdown"
                )
            else:
                await bot.send_message(
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                    text=body,
                    parse_mode="Markdown"
                )
        except Exception as e:
            print(f"Ошибка публикации карточки в топик {thread_id}: {e}")
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                    text=body
                )
            except Exception as ex:
                print(f"Критическая ошибка отправки: {ex}")

        await asyncio.sleep(0.5)

async def publish_dynamic_digest(bot: Bot, chat_id: int, thread_id: int, discovered_data):
    if not discovered_data or not discovered_data.clusters:
        return

    lines = ["✨ **ДНЕВНОЙ МИКС (ВНЕ ОСНОВНЫХ РУБРИК)**\n"]
    for cluster in discovered_data.clusters:
        lines.append(f"**{cluster.topic_name}**")
        for point in cluster.summary_points:
            lines.append(f"• {point}")
        lines.append("")

    full_text = "\n".join(lines)
    try:
        await bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=full_text,
            parse_mode="Markdown",
        )
    except Exception as e:
        print(f"Ошибка отправки дайджеста Other: {e}")