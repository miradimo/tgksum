import re
from datetime import datetime, timedelta
from typing import Optional, Tuple
import aiohttp

import config
from vector_store import VectorStore


class RAGAssistant:
    def __init__(self):
        self.store = VectorStore()
        self.ollama_url = getattr(config, "OLLAMA_URL", "http://localhost:11434")
        # Используем модель из config или дефолтную qwen2.5:14b
        self.model_name = getattr(config, "SUMMARIZER_MODEL", "qwen2.5:14b")

    def _extract_time_filter(self, query: str) -> Tuple[str, Optional[datetime]]:
        """
        Простой парсер временных ограничений из текста вопроса.
        Пример: 'что писали за последние 3 дня про ИИ' -> фильтр date_from = now - 3 days
        """
        now = datetime.utcnow()
        days_match = re.search(r"за\s+(?:последние|прошедшие)?\s*(\d+)\s*(?:дн|дня|дней)", query, re.IGNORECASE)
        if days_match:
            days = int(days_match.group(1))
            return query, now - timedelta(days=days)

        if re.search(r"за\s+(?:эту|последнюю)\s+неделю", query, re.IGNORECASE):
            return query, now - timedelta(days=7)

        if re.search(r"(?:за\s+сегодня|сегодня)", query, re.IGNORECASE):
            return query, now - timedelta(days=1)

        return query, None

    async def answer_question(self, user_query: str) -> str:
        """Поиск по базе и генерация ответа с цитированием ссылок."""
        clean_query, date_from = self._extract_time_filter(user_query)

        # 1. Поиск релевантных фрагментов в Qdrant
        posts = await self.store.search_hybrid(
            query=clean_query,
            date_from=date_from,
            limit=6
        )

        if not posts:
            return "В архиве каналов не нашлось публикаций по вашему запросу."

        # 2. Формирование контекста для LLM
        context_blocks = []
        for i, p in enumerate(posts, 1):
            source_link = f"[{p['channel_title']}]({p['message_link']})" if p.get("message_link") else p['channel_title']
            block = (
                f"--- Документ {i} ---\n"
                f"Источник: {source_link}\n"
                f"Дата публикации: {p.get('date_str', 'не указана')}\n"
                f"Текст:\n{p['text']}\n"
            )
            context_blocks.append(block)

        context_text = "\n".join(context_blocks)

        # 3. Системный промпт со строгим цитированием
        system_prompt = (
            "Ты — аналитический AI-ассистент по архиву новостей Telegram-каналов.\n"
            "Твоя задача — ответить на вопрос пользователя, опираясь ИСКЛЮЧИТЕЛЬНО на предоставленные фрагменты постов.\n\n"
            "Правила:\n"
            "1. Не придумывай факты. Если в контексте нет ответа, прямо скажи об этом.\n"
            "2. Обязательно ссылайся на источники фактов! Для каждой упомянутой новости или тезиса укажи кликабельную ссылку в формате Markdown: [Название Канала](ссылка_на_сообщение).\n"
            "3. Пиши лаконично, структурированно (используй списки и выделение ключевых моментов).\n"
            "4. Отвечай на русском языке."
        )

        user_prompt = (
            f"Контекст из архива постов:\n{context_text}\n\n"
            f"Вопрос пользователя: {user_query}\n\n"
            f"Ответ:"
        )

        # 4. Запрос к Ollama API
        async with aiohttp.ClientSession() as session:
            payload = {
                "model": self.model_name,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "stream": False,
                "options": {
                    "temperature": 0.2,
                }
            }
            try:
                async with session.post(f"{self.ollama_url}/api/chat", json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("message", {}).get("content", "Не удалось сгенерировать ответ.")
                    else:
                        err = await resp.text()
                        return f"Ошибка при обращении к Ollama: {resp.status} ({err})"
            except Exception as e:
                return f"Не удалось связаться с Ollama ({self.ollama_url}): {e}"