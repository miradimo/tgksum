import json
import re
import aiohttp
from typing import List, Dict, Any
import config
from clusterer import EventClusterer


class DynamicSummarizer:
    def __init__(self):
        self.clusterer = EventClusterer()
        self.ollama_url = getattr(config, "OLLAMA_URL", "http://localhost:11434")
        self.model_name = getattr(config, "SUMMARIZER_MODEL", "qwen2.5:14b")

    async def _summarize_cluster(self, cluster_messages: List[Dict[str, Any]], session: aiohttp.ClientSession) -> Dict[str, Any]:
        """Генерирует структурированную карточку события для кластера постов."""
        # Собираем тексты постов кластера
        posts_text = []
        sources = []
        for idx, m in enumerate(cluster_messages, 1):
            src_name = m.get("channel_title") or "Канал"
            link = m.get("message_link") or ""
            sources.append({"channel_title": src_name, "message_link": link})
            posts_text.append(f"[{idx}] Источник: {src_name}\nТекст: {m.get('text', '')}")

        combined_input = "\n\n".join(posts_text)

        prompt = f"""Ты — главный редактор персонального новостного издания.
Ниже представлена пачка постов из разных Telegram-каналов, посвященных одному событию.

Твоя задача — объединить их в одно комплексное событие и выдать строгий JSON-объект.

Требования к полям JSON:
- "story_title": Емкий, цепляющий заголовок новости (до 10 слов).
- "dynamic_tag": Тематический тег, определяющий суть темы (например: "ИИ и Нейросети", "Геополитика", "Макроэкономика", "Разработка", "Авто").
- "summary": Полная фактологическая выжимка новости. Если разные каналы сообщают разные детали, сопоставь их.
- "quote": Если в текстах есть важная прямая речь или яркая цитата спикера/лидера, приведи её дословно (иначе null).
- "code_snippet": Если в тексте обсуждается код, библиотека, консольная команда или конфигурация, выдели ключевой фрагмент кода (иначе null).

Посты:
{combined_input}

Выведи ТОЛЬКО валидный JSON без markdown-кавычек и пояснений по следующей схеме:
{{
  "story_title": "...",
  "dynamic_tag": "...",
  "summary": "...",
  "quote": null,
  "code_snippet": null
}}"""

        payload = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "format": "json",
            "stream": False,
            "options": {"temperature": 0.2}
        }

        try:
            async with session.post(f"{self.ollama_url}/api/chat", json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    raw_text = data.get("message", {}).get("content", "{}")
                    parsed = json.loads(raw_text)
                    parsed["sources"] = sources
                    # Выбираем медиа из первого попавшегося поста кластера, если есть
                    parsed["media_path"] = next((m.get("media_path") for m in cluster_messages if m.get("media_path")), None)
                    return parsed
        except Exception as e:
            print(f"Ошибка генерации сводки для кластера: {e}")

        # Fallback при сбое
        first_post = cluster_messages[0]
        return {
            "story_title": first_post.get("text", "")[:60] + "...",
            "dynamic_tag": "Новости",
            "summary": first_post.get("text", ""),
            "quote": None,
            "code_snippet": None,
            "sources": sources,
            "media_path": None
        }

    async def process_feed(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Глобально кластеризует все входящие посты и создает список новостных событий."""
        if not messages:
            return []

        # 1. Глобальная кластеризация без деления на темы
        clusters = self.clusterer.cluster_messages(messages)
        print(f"Кластеризация завершена: {len(messages)} постов объединено в {len(clusters)} событий.")

        # 2. Сортируем кластеры по размеру (события с наибольшим числом источников идут первыми)
        clusters.sort(key=lambda c: len(c), reverse=True)

        stories = []
        async with aiohttp.ClientSession() as session:
            for idx, cluster in enumerate(clusters, 1):
                print(f"Генерация карточки для события {idx}/{len(clusters)} ({len(cluster)} источников)...")
                story = await self._summarize_cluster(cluster, session)
                stories.append(story)

        return stories