import asyncio
import json
from typing import Literal
import aiosqlite
import ollama
from pydantic import BaseModel, Field

from config import DB_NAME, MODEL_CLASSIFIER

class NewsCategory(BaseModel):
    topic: Literal[
        "svo_military",
        "geopolitics",
        "tech_ai",
        "finance",
        "other",
        "spam",
    ]
    reason: str = Field(description="Краткая причина выбора категории на русском языке")

async def _classify_single_post(client: ollama.AsyncClient, sem: asyncio.Semaphore, post_id: int, text: str, db: aiosqlite.Connection):
    prompt = f"""Классифицируй новостной пост строго в одну из категорий:
- svo_military (СВО, военные действия, фронт, техника, обстрелы, ПВО)
- geopolitics (Международные отношения, дипломатия, переговоры, санкции, внешняя политика)
- tech_ai (Технологии, IT, софт, нейросети, ИИ, гаджеты, наука)
- finance (Экономика, рынки, акции, ключевая ставка, криптовалюты, бизнес)
- other (Любые другие интересные новости: игры, кино, культура, спорт)
- spam (Реклама, скам, реферальные ссылки, кликбейт, промокоды, призывы подписаться)

Поле reason заполни на русском языке.

Текст:
{text}"""

    async with sem:
        try:
            response = await client.chat(
                model=MODEL_CLASSIFIER,
                messages=[{"role": "user", "content": prompt}],
                format=NewsCategory.model_json_schema(),
                options={"temperature": 0.1},
            )
            data = json.loads(response["message"]["content"])
            topic = data["topic"]
            
            # Если это спам, помечаем его сразу обработанным и не подлежащим суммаризации
            is_sum = 1 if topic == "spam" else 0

            await db.execute(
                "UPDATE messages SET topic = ?, is_processed = 1, is_summarized = ? WHERE id = ?",
                (topic, is_sum, post_id),
            )
            print(f"Пост #{post_id} -> {topic} ({data['reason']})")
        except Exception as e:
            print(f"Ошибка классификации поста #{post_id}: {e}")

async def classify_unprocessed(concurrency: int = 4):
    client = ollama.AsyncClient()
    sem = asyncio.Semaphore(concurrency)

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT id, text FROM messages WHERE is_processed = 0 AND text != ''"
        ) as cursor:
            rows = await cursor.fetchall()

        if not rows:
            print("Нет новых постов для классификации.")
            return

        print(f"Классификация {len(rows)} постов (параллельность: {concurrency})...")
        tasks = [
            _classify_single_post(client, sem, post_id, text, db)
            for post_id, text in rows
        ]
        await asyncio.gather(*tasks)
        await db.commit()

if __name__ == "__main__":
    asyncio.run(classify_unprocessed())