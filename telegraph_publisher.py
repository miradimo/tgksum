import aiohttp
from typing import List, Dict, Any, Optional
import os
import config

TELEGRAPH_TOKEN_FILE = ".telegraph_token"

class TelegraphPublisher:
    def __init__(self, author_name: str = "Personal AI Digest"):
        self.author_name = author_name
        self.access_token: Optional[str] = None

    async def _ensure_account(self, session: aiohttp.ClientSession) -> str:
        """Получает существующий токен Telegraph или регистрирует новый аккаунт."""
        if os.path.exists(TELEGRAPH_TOKEN_FILE):
            with open(TELEGRAPH_TOKEN_FILE, "r", encoding="utf-8") as f:
                self.access_token = f.read().strip()
                if self.access_token:
                    return self.access_token

        # Регистрируем новый аккаунт в Telegraph
        url = "https://api.telegra.ph/createAccount"
        params = {
            "short_name": "tgksum",
            "author_name": self.author_name
        }
        async with session.get(url, params=params) as resp:
            data = await resp.json()
            if data.get("ok"):
                self.access_token = data["result"]["access_token"]
                with open(TELEGRAPH_TOKEN_FILE, "w", encoding="utf-8") as f:
                    f.write(self.access_token)
                return self.access_token
            else:
                raise RuntimeError(f"Ошибка создания Telegraph аккаунта: {data}")

    def _build_html_content(self, stories: List[Dict[str, Any]]) -> str:
            """
            Верстает красивый лонгрид для Telegraph с учетом поддерживаемых тегов:
            - Рубрики через <h3>
            - Заголовки новостей через <h4> или жирный шрифт
            - Дедупликация источников
            - Разделители <hr/>
            """
            html_parts = []
            
            html_parts.append(
                f"<p><i>Персональный медиа-дайджест. Собрано и сгруппировано событий: <b>{len(stories)}</b>.</i></p><hr/>"
            )

            # Группируем истории по динамическим тегам
            grouped: Dict[str, List[Dict[str, Any]]] = {}
            for s in stories:
                raw_tag = s.get("dynamic_tag") or "Новости"
                tag = raw_tag.strip("# ").capitalize()
                grouped.setdefault(tag, []).append(s)

            for tag, tag_stories in grouped.items():
                # Рубрика (в Telegraph H3 — главный заголовок разделов)
                html_parts.append(f"<h3>🏷️ {tag}</h3>")

                for story in tag_stories:
                    title = story.get("story_title", "").strip()
                    html_parts.append(f"<h4>📌 {title}</h4>")

                    # Текст новости
                    summary_text = story.get("summary", "").strip()
                    paragraphs = summary_text.split("\n\n")
                    for p in paragraphs:
                        clean_p = p.strip().replace("\n", "<br/>")
                        if clean_p:
                            html_parts.append(f"<p>{clean_p}</p>")

                    # Цитата (если есть)
                    if story.get("quote"):
                        clean_quote = str(story["quote"]).strip()
                        if clean_quote and clean_quote.lower() != "null":
                            html_parts.append(f"<blockquote>«{clean_quote}»</blockquote>")

                    # Блок кода (если есть)
                    if story.get("code_snippet"):
                        clean_code = str(story["code_snippet"]).strip()
                        if clean_code and clean_code.lower() != "null":
                            html_parts.append(f"<pre><code>{clean_code}</code></pre>")

                    # Дедупликация источников: один канал показывается один раз
                    unique_sources: Dict[str, str] = {}
                    for src in story.get("sources", []):
                        ch_title = src.get("channel_title") or "Канал"
                        ch_link = src.get("message_link") or ""
                        # Если еще нет этого канала или появился линк
                        if ch_title not in unique_sources or (ch_link and not unique_sources[ch_title]):
                            unique_sources[ch_title] = ch_link

                    if unique_sources:
                        src_links = []
                        for name, link in unique_sources.items():
                            if link:
                                src_links.append(f"<a href='{link}'>{name}</a>")
                            else:
                                src_links.append(name)
                        html_parts.append(f"<p>📍 <b>Источники:</b> {', '.join(src_links)}</p>")

                    html_parts.append("<hr/>")

            return "".join(html_parts)

    async def publish_digest(self, title: str, stories: List[Dict[str, Any]]) -> str:
        """Публикует дайджест и возвращает URL статьи (Instant View в Telegram)."""
        async with aiohttp.ClientSession() as session:
            token = await self._ensure_account(session)
            html_content = self._build_html_content(stories)

            # Telegraph принимает контент в формате DOM nodes
            # Конвертируем через telegraph endpoint /createPage
            # Чтобы передать простой html, Telegraph API поддерживает параметр return_content
            # и разметку через json nodes. Для простоты используем библиотеку telegraph-nodes
            # или стандартный метод createPage
            
            # Стандартная конвертация простого HTML в Telegraph JSON-дерево
            from html.parser import HTMLParser

            class MiniHTMLToTelegraph(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.nodes = []
                    self.stack = [self.nodes]

                def handle_starttag(self, tag, attrs):
                    node = {"tag": tag}
                    if attrs:
                        node["attrs"] = dict(attrs)
                    node["children"] = []
                    self.stack[-1].append(node)
                    if tag not in ("br", "hr", "img"):
                        self.stack.append(node["children"])

                def handle_endtag(self, tag):
                    if tag not in ("br", "hr", "img") and len(self.stack) > 1:
                        self.stack.pop()

                def handle_data(self, data):
                    if data:
                        self.stack[-1].append(data)

            parser = MiniHTMLToTelegraph()
            parser.feed(html_content)
            content_nodes = parser.nodes

            import json
            payload = {
                "access_token": token,
                "title": title,
                "author_name": self.author_name,
                "content": json.dumps(content_nodes),
                "return_content": False
            }

            url = "https://api.telegra.ph/createPage"
            async with session.post(url, json=payload) as resp:
                res = await resp.json()
                if res.get("ok"):
                    return res["result"]["url"]
                else:
                    raise RuntimeError(f"Ошибка публикации Telegraph: {res}")