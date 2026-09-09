from typing import List
import aiosqlite
import ollama
from pydantic import BaseModel, Field

from config import DB_NAME, MODEL_SUMMARIZER

class DiscoveredCluster(BaseModel):
    topic_name: str = Field(description="Название темы с эмодзи, например: '🎮 Игры' или '🎬 Кино'")
    summary_points: List[str] = Field(description="2-4 кратких тезиса с главными событиями")

class DiscoveredTopicsResponse(BaseModel):
    clusters: List[DiscoveredCluster]

async def process_other_news() -> DiscoveredTopicsResponse | None:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("""
            SELECT id, channel_title, text 
            FROM messages 
            WHERE topic = 'other' AND is_summarized = 0
            ORDER BY id DESC LIMIT 20
        """) as cursor:
            posts = await cursor.fetchall()

    if not posts:
        return None

    formatted_posts = "\n\n".join(
        f"[{idx + 1}] Источник: {ch}\nТекст: {txt}"
        for idx, (_, ch, txt) in enumerate(posts)
    )

    prompt = f"""Перед тобой посты, не вошедшие в основные темы политики и IT.
Сгруппируй их по 1-4 смысловым рубрикам (Игры, Культура, Наука, Общество и т.д.) и напиши краткие тезисы.

Посты:
{formatted_posts}"""

    client = ollama.AsyncClient()
    try:
        response = await client.chat(
            model=MODEL_SUMMARIZER,
            messages=[{"role": "user", "content": prompt}],
            format=DiscoveredTopicsResponse.model_json_schema(),
            options={"temperature": 0.2},
        )
        data = DiscoveredTopicsResponse.model_validate_json(response["message"]["content"])
    except Exception as e:
        print(f"Ошибка обработки dynamic topics: {e}")
        return None

    post_ids = [p[0] for p in posts]
    async with aiosqlite.connect(DB_NAME) as db:
        placeholders = ",".join("?" for _ in post_ids)
        await db.execute(
            f"UPDATE messages SET is_summarized = 1 WHERE id IN ({placeholders})",
            post_ids,
        )
        await db.commit()

    return data