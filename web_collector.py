import asyncio
import re
import aiosqlite
from bs4 import BeautifulSoup
import httpx

DB_NAME = "news.db"

TARGET_CHANNELS = [
    "rian_ru",
    "rbc_news",
    "habr_com",
    "mod_russia",
    "pozyvnoy_leon",
    "deadlockrf",
    "military_zB",
]


async def fetch_channel_posts(
    channel: str, client: httpx.AsyncClient
) -> list[dict]:
  url = f"https://t.me/s/{channel}"
  try:
    response = await client.get(url, follow_redirects=True)
  except httpx.ConnectTimeout:
    print(f"  [!] Таймаут: сервер t.me не ответил при запросе канала @{channel}.")
    return []
  except httpx.RequestError as e:
    print(f"  [!] Сетевая ошибка при запросе @{channel}: {e}")
    return []

  if response.status_code != 200:
    print(f"  [!] Ошибка загрузки @{channel}: HTTP статус {response.status_code}")
    return []

  soup = BeautifulSoup(response.text, "html.parser")
  messages_raw = soup.find_all("div", class_="tgme_widget_message")
  parsed_posts = []

  for msg in messages_raw:
    text_div = msg.find("div", class_="tgme_widget_message_text")
    if not text_div:
      continue

    text = text_div.get_text(separator="\n", strip=True)
    if len(text) < 40:
      continue

    # Извлечение ссылки на картинку
    media_url = None
    photo_wrap = msg.find("a", class_="tgme_widget_message_photo_wrap")
    if photo_wrap and "style" in photo_wrap.attrs:
      match = re.search(r"url\(['\"]?(.*?)['\"]?\)", photo_wrap["style"])
      if match:
        media_url = match.group(1)

    parsed_posts.append({
        "channel": channel,
        "text": text,
        "media_url": media_url,
    })

  return parsed_posts


async def save_posts_to_db(posts: list[dict]):
  async with aiosqlite.connect(DB_NAME) as db:
    inserted_count = 0
    for p in posts:
      cursor = await db.execute(
          """
                INSERT OR IGNORE INTO messages (channel_title, text, media_url, is_processed, is_summarized) 
                VALUES (?, ?, ?, 0, 0)
            """,
          (p["channel"], p["text"], p["media_url"]),
      )
      if cursor.rowcount > 0:
        inserted_count += 1

    await db.commit()
    return inserted_count


async def run_collector():
  headers = {
      "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
          " like Gecko) Chrome/120.0.0.0 Safari/537.36"
      )
  }

  async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
    for channel in TARGET_CHANNELS:
      print(f"Парсинг канала @{channel}...")
      posts = await fetch_channel_posts(channel, client)
      added = await save_posts_to_db(posts)
      print(f"  -> Найдено постов: {len(posts)}, новых добавлено в БД: {added}")


if __name__ == "__main__":
  asyncio.run(run_collector())