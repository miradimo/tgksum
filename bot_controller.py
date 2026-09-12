import asyncio
from datetime import datetime
import os
import re
from typing import Optional
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
import config
from database import init_db, get_channels_list, toggle_channel_status, get_db_stats
from telethon_collector import collect_posts_from_channels, sync_user_channels
from classifier import classify_unprocessed
from summarizer import generate_all_cards
from dynamic_topics import process_other_news
from publisher import publish_cards_to_topic, publish_dynamic_digest
from rag_assistant import RAGAssistant
from database import (
    get_messages_for_digest, 
    init_settings_db, 
    get_user_settings, 
    update_user_setting
)
from dynamic_summarizer import DynamicSummarizer
from telegraph_publisher import TelegraphPublisher

rag_assistant = RAGAssistant()
pipeline_lock = asyncio.Lock()

summarizer = DynamicSummarizer()
publisher = TelegraphPublisher(author_name="TGKSum Personal Feed")

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

def get_main_reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📰 Сформировать дайджест")],
            [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="🔍 Задать вопрос (RAG)")]
        ],
        resize_keyboard=True
    )

def render_settings_keyboard(s: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Формирует текст и клавиатуру меню настроек с актуальными тумблерами."""
    # Обозначение текущей глубины
    if s["depth_type"] == "hours":
        depth_label = f"🕒 {s['depth_val']} ч."
    else:
        depth_label = f"🔢 {s['depth_val']} постов"

    major_label = "🔥 Только главное (2+ источника)" if s["only_major"] else "🌐 Все новости"
    quotes_label = "✅ Вкл" if s["show_quotes"] else "❌ Выкл"
    code_label = "✅ Вкл" if s["show_code"] else "❌ Выкл"

    text = (
        "⚙️ *Панель настроек вашего дайджеста*\n\n"
        f"• *Глубина сбора:* `{depth_label}`\n"
        f"• *Фильтр сюжетов:* `{major_label}`\n"
        f"• *Цитаты в статьях:* `{quotes_label}`\n"
        f"• *Код и вставки:* `{code_label}`\n\n"
        "_Нажимайте на кнопки ниже, чтобы изменить параметры:_"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"⏱ Глубина: {depth_label}", callback_data="set_cycle_depth")],
            [InlineKeyboardButton(text=f"Фильтр: {major_label}", callback_data="set_toggle_major")],
            [
                InlineKeyboardButton(text=f"Цитаты: {quotes_label}", callback_data="set_toggle_quotes"),
                InlineKeyboardButton(text=f"Код: {code_label}", callback_data="set_toggle_code"),
            ],
            [InlineKeyboardButton(text="◀️ Закрыть меню", callback_data="set_close")]
        ]
    )
    return text, kb

# Хэндлер на команду /settings или нажатие кнопки "⚙️ Настройки"
@dp.message(F.text == "⚙️ Настройки")
@dp.message(Command("settings"))
async def cmd_settings(message: types.Message):
    await init_settings_db(config.DB_NAME)
    s = await get_user_settings(config.DB_NAME, message.from_user.id)
    text, kb = render_settings_keyboard(s)
    await message.answer(text, reply_markup=kb, parse_mode="Markdown")


# Обработка нажатий на тумблеры в настройках
@dp.callback_query(F.data.startswith("set_"))
async def cb_settings(call: types.CallbackQuery):
    user_id = call.from_user.id
    action = call.data
    s = await get_user_settings(config.DB_NAME, user_id)

    if action == "set_close":
        await call.message.delete()
        return

    # Циклическое переключение глубины: 12ч -> 24ч -> 72ч -> 30 постов -> 70 постов -> 12ч
    if action == "set_cycle_depth":
        cycle = [
            ("hours", 12),
            ("hours", 24),
            ("hours", 72),
            ("count", 30),
            ("count", 70),
        ]
        curr = (s["depth_type"], s["depth_val"])
        idx = cycle.index(curr) if curr in cycle else 1
        next_type, next_val = cycle[(idx + 1) % len(cycle)]
        await update_user_setting(config.DB_NAME, user_id, "depth_type", next_type)
        await update_user_setting(config.DB_NAME, user_id, "depth_val", next_val)

    elif action == "set_toggle_major":
        await update_user_setting(config.DB_NAME, user_id, "only_major", 0 if s["only_major"] else 1)

    elif action == "set_toggle_quotes":
        await update_user_setting(config.DB_NAME, user_id, "show_quotes", 0 if s["show_quotes"] else 1)

    elif action == "set_toggle_code":
        await update_user_setting(config.DB_NAME, user_id, "show_code", 0 if s["show_code"] else 1)

    # Обновляем клавиатуру и текст на лету
    updated_s = await get_user_settings(config.DB_NAME, user_id)
    new_text, new_kb = render_settings_keyboard(updated_s)
    await call.message.edit_text(new_text, reply_markup=new_kb, parse_mode="Markdown")
    await call.answer()

def build_message_link(channel_username: str, channel_id: int, message_id: int) -> str:
    if channel_username:
        clean_user = channel_username.lstrip("@")
        return f"https://t.me/{clean_user}/{message_id}"
    if channel_id:
        clean_id = str(channel_id).replace("-100", "").lstrip("-")
        return f"https://t.me/c/{clean_id}/{message_id}"
    return ""

async def run_digest_pipeline(message: types.Message, limit: Optional[int] = None, hours: Optional[int] = None):
    status_msg = await message.answer("⏳ *Собираю посты и формирую кластеры событий...*", parse_mode="Markdown")

    try:
        raw_posts = await get_messages_for_digest(config.DB_NAME, limit=limit, hours=hours)
        if not raw_posts:
            await status_msg.edit_text("За выбранный период не найдено подходящих постов.")
            return

        # Формируем ссылки на сообщения
        for p in raw_posts:
            p["message_link"] = build_message_link(p.get("channel_username"), p.get("channel_id"), p.get("message_id"))

        await status_msg.edit_text(f"🧠 *Кластеризую {len(raw_posts)} постов и генерирую саммари через LLM...*", parse_mode="Markdown")
        stories = await summarizer.process_feed(raw_posts)

        await status_msg.edit_text("🎨 *Верстаю статью в Telegraph...*", parse_mode="Markdown")
        today_str = datetime.now().strftime("%d.%m.%Y %H:%M")
        telegraph_url = await publisher.publish_digest(f"Дайджест — {today_str}", stories)

        # Клавиатура с кнопкой Instant View
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📖 Читать полный дайджест", url=telegraph_url)]
            ]
        )

        preview_text = (
            f"📰 *Ваш персональный дайджест готов!*\n\n"
            f"• Проанализировано постов: *{len(raw_posts)}*\n"
            f"• Выделено ключевых сюжетов: *{len(stories)}*\n\n"
            f"_Нажмите кнопку ниже для открытия в режиме Instant View:_"
        )

        await status_msg.delete()
        await message.answer(preview_text, reply_markup=kb, parse_mode="Markdown")

    except Exception as e:
        await status_msg.edit_text(f"Произошла ошибка при генерации дайджеста: {e}")

def check_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

@dp.message(F.text == "📰 Сформировать дайджест")
async def cmd_quick_digest(message: types.Message):
    s = await get_user_settings(config.DB_NAME, message.from_user.id)
    
    hours = s["depth_val"] if s["depth_type"] == "hours" else None
    limit = s["depth_val"] if s["depth_type"] == "count" else None
    
    # Запускаем генерацию с индивидуальными настройками пользователя
    await run_digest_pipeline(message, limit=limit, hours=hours)


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await init_settings_db(config.DB_NAME)
    await message.answer(
        "👋 *Привет! Я ваш персональный AI-ассистент по Telegram-каналам.*\n\n"
        "• Нажмите *«📰 Сформировать дайджест»*, чтобы получить сводку.\n"
        "• В разделе *«⚙️ Настройки»* можно выбрать глубину сбора и фильтры.\n"
        "• Задавайте любые вопросы по архиву постов через команду `/ask` или кнопку поиска.",
        reply_markup=get_main_reply_kb(),
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


@dp.message(Command("digest"))
async def cmd_digest(message: types.Message):
    args = message.text.replace("/digest", "").strip().lower()

    # Если передан аргумент в чате
    if args:
        # Вариант по часам: 12h, 24h
        match_h = re.match(r"^(\d+)\s*h$", args)
        if match_h:
            return await run_digest_pipeline(message, hours=int(match_h.group(1)))

        # Вариант по дням: 2d, 3d
        match_d = re.match(r"^(\d+)\s*d$", args)
        if match_d:
            return await run_digest_pipeline(message, hours=int(match_d.group(1)) * 24)

        # Вариант по количеству: 30, 50, 100
        if args.isdigit():
            return await run_digest_pipeline(message, limit=int(args))

    # Если аргументов нет — предлагаем удобные кнопки
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🕒 12 часов", callback_data="dig_h_12"),
                InlineKeyboardButton(text="📅 24 часа", callback_data="dig_h_24"),
                InlineKeyboardButton(text="📆 3 дня", callback_data="dig_h_72"),
            ],
            [
                InlineKeyboardButton(text="🔢 30 постов", callback_data="dig_cnt_30"),
                InlineKeyboardButton(text="🔢 70 постов", callback_data="dig_cnt_70"),
            ]
        ]
    )
    await message.answer("Выберите глубину для формирования дайджеста:", reply_markup=kb)

@dp.message(Command("ask"))
async def cmd_ask(message: types.Message):
    # Извлекаем текст вопроса после команды /ask
    query = message.text.replace("/ask", "", 1).strip()

    if not query:
        await message.answer(
            "Пожалуйста, укажите вопрос после команды.\n\n"
            "Пример:\n`/ask Что писали за последние 3 дня про санкции на экспорт чипов?`",
            parse_mode="Markdown",
        )
        return

    status_msg = await message.answer("🔍 *Ищу в архиве и анализирую...*", parse_mode="Markdown")

    try:
        response = await rag_assistant.answer_question(query)
        # Отправляем ответ без генерации предпросмотра ссылок, чтобы не засорять чат
        await status_msg.edit_text(
            response,
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
    except Exception as e:
        await status_msg.edit_text(f"Произошла ошибка при обработке запроса: {e}")

@dp.callback_query(F.data.startswith("dig_"))
async def cb_digest(call: types.CallbackQuery):
    await call.message.delete()
    code = call.data

    if code == "dig_h_12":
        await run_digest_pipeline(call.message, hours=12)
    elif code == "dig_h_24":
        await run_digest_pipeline(call.message, hours=24)
    elif code == "dig_h_72":
        await run_digest_pipeline(call.message, hours=72)
    elif code == "dig_cnt_30":
        await run_digest_pipeline(call.message, limit=30)
    elif code == "dig_cnt_70":
        await run_digest_pipeline(call.message, limit=70)

async def main():
    await init_db()
    print(" Бот-контроллер успешно запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
