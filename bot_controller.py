import asyncio
import os
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telethon import TelegramClient

from config import BOT_TOKEN, ADMIN_ID, CHAT_ID, TOPIC_THREADS, API_ID, API_HASH, SESSION_NAME
from database import init_db, get_channels_list, toggle_channel_status, get_db_stats
from telethon_collector import collect_posts_from_channels, sync_user_channels
from classifier import classify_unprocessed
from summarizer import generate_all_cards
from dynamic_topics import process_other_news
from publisher import publish_cards_to_topic, publish_dynamic_digest

pipeline_lock = asyncio.Lock()

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Главное меню с кнопками
main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🚀 Запустить полный дайджест")],
        [KeyboardButton(text="🔄 Синхронизировать каналы"), KeyboardButton(text="📑 Список каналов")],
        [KeyboardButton(text="📊 Статистика базы")]
    ],
    resize_keyboard=True
)

def check_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    if not check_admin(message.from_user.id):
        await message.answer(" Доступ запрещен.")
        return

    await message.answer(
        "👋 **Панель управления AI-Дайджестом**\n\n"
        "Нажмите кнопку внизу или используйте команды:\n"
        "• `/run` — запустить сбор и публикацию\n"
        "• `/sync` — найти новые каналы в аккаунте\n"
        "• `/channels` — настроить каналы для сбора\n"
        "• `/stats` — статус базы данных",
        reply_markup=main_keyboard,
        parse_mode="Markdown"
    )


# 1. СТАТИСТИКА
@dp.message(Command("stats"))
@dp.message(F.text.contains("Статистика базы"))
async def show_stats(message: types.Message):
    if not check_admin(message.from_user.id):
        return

    stats = await get_db_stats()
    text = (
        f"📊 **Текущее состояние системы:**\n\n"
        f"• Активных каналов в сборе: **{stats['active_channels']}**\n"
        f"• Всего постов в БД: **{stats['total_messages']}**\n"
        f"• Ожидают классификации: **{stats['unprocessed']}**"
    )
    await message.answer(text, parse_mode="Markdown")


# 2. СИНХРОНИЗАЦИЯ КАНАЛОВ
@dp.message(Command("sync"))
@dp.message(F.text.contains("Синхронизировать каналы"))
async def handle_sync(message: types.Message):
    if not check_admin(message.from_user.id):
        return

    status_msg = await message.answer("⏳ Подключение к Telethon и поиск каналов...")
    try:
        async with TelegramClient(SESSION_NAME, API_ID, API_HASH) as client:
            await sync_user_channels(client)
        await status_msg.edit_text("✅ Список каналов вашего аккаунта успешно обновлен в БД!\nОткройте «📑 Список каналов», чтобы выбрать нужные.")
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка синхронизации: `{e}`", parse_mode="Markdown")


# 3. СПИСОК И УПРАВЛЕНИЕ КАНАЛАМИ
@dp.message(Command("channels"))
@dp.message(F.text.contains("Список каналов"))
async def list_channels(message: types.Message):
    if not check_admin(message.from_user.id):
        return

    channels = await get_channels_list()
    if not channels:
        await message.answer("Каналы еще не найдены. Нажмите сначала кнопку **🔄 Синхронизировать каналы**.")
        return

    builder = []
    for ch_id, title, is_private, is_active in channels[:40]:
        status_icon = "🟢" if is_active else "🔴"
        type_icon = "🔒" if is_private else "🌐"
        btn_text = f"{status_icon} {type_icon} {title[:22]}"
        builder.append([InlineKeyboardButton(text=btn_text, callback_data=f"toggle_ch:{ch_id}")])

    kb = InlineKeyboardMarkup(inline_keyboard=builder)
    await message.answer(
        "📑 **Управление каналами:**\n"
        "Нажмите на нужные каналы, чтобы включить (🟢) или выключить (🔴) их из сбора:\n"
        "_(Активные каналы собираются в дайджест, отключенные игнорируются)_",
        reply_markup=kb,
        parse_mode="Markdown"
    )


@dp.callback_query(F.data.startswith("toggle_ch:"))
async def callback_toggle_channel(callback: types.CallbackQuery):
    if not check_admin(callback.from_user.id):
        await callback.answer("Нет прав.", show_alert=True)
        return

    # Извлекаем ID канала, отрезая префикс toggle_ch:
    ch_id_str = callback.data.replace("toggle_ch:", "")
    ch_id = int(ch_id_str)

    new_state = await toggle_channel_status(ch_id)
    status_str = "включен в сбор 🟢" if new_state else "отключен 🔴"
    await callback.answer(f"Канал {status_str}")

    channels = await get_channels_list()
    builder = []
    for cid, title, is_private, is_active in channels[:40]:
        status_icon = "🟢" if is_active else "🔴"
        type_icon = "🔒" if is_private else "🌐"
        btn_text = f"{status_icon} {type_icon} {title[:22]}"
        builder.append([InlineKeyboardButton(text=btn_text, callback_data=f"toggle_ch:{cid}")])

    await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=builder))


# 4. ЗАПУСК ДАЙДЖЕСТА
@dp.message(Command("run"))
@dp.message(F.text.contains("Запустить полный дайджест"))
async def run_full_pipeline_cmd(message: types.Message):
    if not check_admin(message.from_user.id):
        return

    if pipeline_lock.locked():
        await message.answer("⚠️ Пайплайн уже выполняется! Дождитесь завершения текущего цикла.")
        return

    async with pipeline_lock:
        status_msg = await message.answer("🚀 **Запуск пайплайна дайджеста**\n\n[1/4] ⏳ Сбор постов из активных каналов...", parse_mode="Markdown")

        try:
            # 1. Сбор постов
            new_posts = await collect_posts_from_channels(limit_per_channel=10)
            await status_msg.edit_text(f"🚀 **Запуск пайплайна дайджеста**\n\n[2/4] 🧠 Собрано новых постов: {new_posts}.\nКлассификация тем через Ollama...", parse_mode="Markdown")

            # 2. Классификация
            await classify_unprocessed(concurrency=4)
            await status_msg.edit_text("🚀 **Запуск пайплайна дайджеста**\n\n[3/4] ✍️ Генерация карточек тем и дайджеста...", parse_mode="Markdown")

            # 3. Суммаризация
            all_cards = await generate_all_cards()
            other_digest = await process_other_news()
            await status_msg.edit_text("🚀 **Запуск пайплайна дайджеста**\n\n[4/4] 📤 Отправка в топики супергруппы...", parse_mode="Markdown")

            # 4. Публикация
            published_count = 0
            for topic, cards in all_cards.items():
                thread_id = TOPIC_THREADS.get(topic)
                if thread_id and cards:
                    await publish_cards_to_topic(bot, CHAT_ID, thread_id, cards)
                    published_count += len(cards)

            if other_digest and TOPIC_THREADS.get("other"):
                await publish_dynamic_digest(bot, CHAT_ID, TOPIC_THREADS["other"], other_digest)
                published_count += 1

            await status_msg.edit_text(
                f"🎉 **Дайджест успешно опубликован!**\n\n"
                f"• Новых постов собрано: **{new_posts}**\n"
                f"• Опубликовано карточек: **{published_count}**",
                parse_mode="Markdown"
            )

        except Exception as e:
            await status_msg.edit_text(f"❌ Ошибка во время выполнения пайплайна:\n`{e}`", parse_mode="Markdown")


async def main():
    await init_db()
    print(" Бот-контроллер успешно запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
