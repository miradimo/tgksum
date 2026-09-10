import asyncio
import uuid
from typing import List, Optional, Dict, Any
import aiosqlite
import ollama
from pydantic import BaseModel, Field

from clusterer import EventClusterer
from config import DB_NAME, MODEL_SUMMARIZER
from database import mark_cluster_summarized

# Инициализируем асинхронный клиент Ollama
ollama_client = ollama.AsyncClient()

# Инициализируем кластеризатор с проверенными параметрами
clusterer = EventClusterer(
    model_name="cointegrated/rubert-tiny2",
    distance_threshold=0.35,
    time_window_hours=18.0
)


class NewsCard(BaseModel):
    headline: str = Field(description="Короткий цепляющий заголовок новости")
    text: str = Field(description="Сжатая суть инфоповода (2-4 предложения), объединяющая факты из всех источников кластера")
    quote: Optional[str] = Field(None, description="Ключевая цитата или яркий тезис, если есть")
    source_post_ids: List[int] = Field(default_factory=list, description="Список ID ВСЕХ постов, вошедших в этот инфоповод")
    source_channels: List[str] = Field(default_factory=list, description="Список названий каналов-источников")
    media_path: Optional[str] = None


class TopicCardsResponse(BaseModel):
    cards: List[NewsCard]


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


async def get_unsummarized_by_topic(topic: str, limit: int = 30) -> List[Dict[str, Any]]:
    """Загружает необработанные посты по теме из БД."""
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT *
            FROM messages
            WHERE is_processed = 1 AND is_summarized = 0 AND topic = ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (topic, limit)
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def summarize_topic_to_cards(topic: str) -> List[NewsCard]:
    # 1. Получаем до 30 не суммаризированных постов нужного топика
    messages = await get_unsummarized_by_topic(topic, limit=30)
    if not messages:
        return []

    # 2. Кластеризуем посты по смыслу и времени публикации
    clusters = clusterer.cluster_messages(messages)
    if not clusters:
        return []

    # Быстрый lookup-словарь постов по ID
    post_map = {m["id"]: m for m in messages}

    # 3. Формируем контекст для Ollama: группируем посты по инфоповодам
    cluster_blocks = []
    for idx, cluster in enumerate(clusters, 1):
        cluster_text = "\n---\n".join([
            f"[Post ID: {m['id']} | Канал: {m['channel_title']}]: {m['text']}"
            for m in cluster
        ])
        cluster_blocks.append(
            f"=== ИНФОПОВОД №{idx} (источников: {len(cluster)}) ===\n{cluster_text}"
        )

    topic_role = TOPIC_CONFIG.get(topic, {}).get("role", "профессионального редактора новостей.")
    system_prompt = (
        f"Ты действуешь в роли {topic_role}\n"
        "Твоя задача — выпускать выверенные, емкие новостные сводки без кликбейта и повторов."
    )

    user_prompt = (
        "Тебе переданы сообщения из Telegram-каналов, уже сгруппированные по отдельным инфоповодам.\n"
        "Сформируй для КАЖДОГО инфоповода ровно одну карточку новости.\n\n"
        "Требования:\n"
        "1. Объединяй факты: если несколько каналов пишут об одном, напиши один связный текст (2-4 предложения).\n"
        "2. В поле source_post_ids обязательно перечисли реальные Post ID всех сообщений этого инфоповода.\n"
        "3. В поле source_channels перечисли названия каналов, написавших об этом.\n\n"
        f"Материалы для обработки:\n\n" + "\n\n".join(cluster_blocks)
    )

    # 4. Запрос к Ollama со структурированным JSON-выводом
    try:
        response = await ollama_client.chat(
            model=MODEL_SUMMARIZER,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            format=TopicCardsResponse.model_json_schema()
        )
        result = TopicCardsResponse.model_validate_json(response.message.content)
    except Exception as e:
        print(f"Ошибка при вызове Ollama для топика [{topic}]: {e}")
        return []

    processed_cards: List[NewsCard] = []

    # 5. Валидация карточек, привязка медиа и сохранение в БД
    for idx, card in enumerate(result.cards):
        # Защита от галлюцинаций ID: если модель забыла ID, берем посты соответствующего кластера
        valid_post_ids = [pid for pid in card.source_post_ids if pid in post_map]
        if not valid_post_ids and idx < len(clusters):
            valid_post_ids = [m["id"] for m in clusters[idx]]
            card.source_post_ids = valid_post_ids

        # Если модель не указала каналы, достаем их из post_map
        if not card.source_channels:
            card.source_channels = list({post_map[pid]["channel_title"] for pid in valid_post_ids if pid in post_map})

        # Привязка первого доступного медиа из постов кластера
        for post_id in valid_post_ids:
            post = post_map.get(post_id)
            if post and post.get("media_path"):
                card.media_path = post["media_path"]
                break

        # Генерируем уникальный ID кластера и помечаем посты в БД как суммаризированные
        cluster_id = str(uuid.uuid4())[:8]
        if valid_post_ids:
            await mark_cluster_summarized(valid_post_ids, cluster_id)

        processed_cards.append(card)

    return processed_cards


async def generate_all_cards() -> dict[str, list[dict]]:
    all_digests = {}
    for topic in TOPIC_CONFIG.keys():
        print(f"Генерация карточек для темы [{topic}]...")
        cards = await summarize_topic_to_cards(topic)
        if cards:
            # Конвертируем Pydantic-объекты NewsCard в обычные словари для publisher.py
            all_digests[topic] = [
                card.model_dump() if hasattr(card, "model_dump") else card.dict()
                for card in cards
            ]
    return all_digests