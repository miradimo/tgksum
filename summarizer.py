import asyncio
import aiosqlite
from typing import List
import ollama
from pydantic import BaseModel, Field

from config import DB_NAME, MODEL_SUMMARIZER

class DigestCard(BaseModel):
    headline: str = Field(description="Локация или субъект (например: 'Купянское направление' или 'Центробанк РФ')")
    text: str = Field(description="Суть события в 2-3 предложениях СТРОГО НА РУССКОМ ЯЗЫКЕ")
    quote: str | None = Field(default=None, description="Прямая цитата, если есть (иначе null)")
    source_post_id: int = Field(description="Точный ID поста из [ID: ...]")

class TopicCardsResponse(BaseModel):
    cards: List[DigestCard]

TOPIC_CONFIG = {
    "svo_military": {
        "title": "⚔️ ХРОНИКА СВО И БЕЗОПАСНОСТЬ",
        "role": "военного аналитика. Излагай сухо, точно, привязывайся к конкретным направлениям и фактам.",
    },
    "geopolitics": {
        "title": "🌐 МЕЖДУНАРОДНАЯ ПАНОРАМА",
        "role": "международного обозревателя. Группируй по странам/союзам. Цитаты выделяй отдельно.",
    },
    "tech_ai": {
        "title": "⚡️ ТЕХНОЛОГИИ И AI",
        "role": "технического редактора. Выделяй суть релизов, моделей и архитектур без маркетинговой воды.",
    },
    "finance": {
        "title": "📊 РЫНКИ И ЭКОНОМИКА",
        "role": "финансового аналитика. Фокусируйся на цифрах, процентных ставках и конкретных решениях регуляторов.",
    },
}

async def summarize_topic_to_cards(topic: str) -> list[dict]:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("""
            SELECT id, channel_title, text, media_path 
            FROM messages 
            WHERE topic = ? AND is_processed = 1 AND is_summarized = 0
            ORDER BY id DESC LIMIT 25
        """, (topic,)) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        return []

    # Исправлено: берем именно row (строку пути к файлу), а не весь кортеж row
    media_map = {row[0]: row for row in rows}

    # Исправлено: подставляем название канала row и текст row
    formatted_posts = "\n\n".join(
        f"[ID: {row[0]}] Источник: {row}\nТекст: {row}"
        for row in rows
    )

    role_desc = TOPIC_CONFIG[topic]["role"]
    prompt = f"""Ты выступаешь в роли {role_desc}
Проанализируй посты и сформируй от 2 до 5 главных уникальных событий.

ТРЕБОВАНИЯ:
1. ЯЗЫК: СТРОГО РУССКИЙ.
2. Для каждого события обязательно укажи точный source_post_id из скобок [ID: ...].
3. Убери воду, кликбейт и дубликаты.

Посты:
{formatted_posts}"""

    client = ollama.AsyncClient()
    try:
        response = await client.chat(
            model=MODEL_SUMMARIZER,
            messages=[{"role": "user", "content": prompt}],
            format=TopicCardsResponse.model_json_schema(),
            options={"temperature": 0.1},
        )
        parsed = TopicCardsResponse.model_validate_json(response["message"]["content"])
    except Exception as e:
        print(f"Ошибка суммаризации темы [{topic}]: {e}")
        return []

    result_cards = []
    for card in parsed.cards:
        result_cards.append({
            "headline": card.headline,
            "text": card.text,
            "quote": card.quote,
            "media_path": media_map.get(card.source_post_id),
        })

    # Помечаем прочитанными все выбранные посты темы
    async with aiosqlite.connect(DB_NAME) as db:
        placeholders = ",".join("?" for _ in rows)
        all_ids = [r[0] for r in rows]
        await db.execute(f"UPDATE messages SET is_summarized = 1 WHERE id IN ({placeholders})", all_ids)
        await db.commit()

    return result_cards

async def generate_all_cards() -> dict[str, list[dict]]:
    all_digests = {}
    for topic in TOPIC_CONFIG.keys():
        print(f"Генерация карточек для темы [{topic}]...")
        cards = await summarize_topic_to_cards(topic)
        if cards:
            all_digests[topic] = cards
    return all_digests