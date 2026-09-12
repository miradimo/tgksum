from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import aiosqlite
from config import DB_NAME

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        # Создаем таблицу каналов
        await db.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                channel_id INTEGER PRIMARY KEY,
                title TEXT,
                username TEXT,
                is_private BOOLEAN,
                is_active BOOLEAN DEFAULT 0
            )
        """)

        # Создаем таблицу сообщений
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER,
                message_id INTEGER,
                channel_title TEXT,
                text TEXT,
                media_path TEXT DEFAULT NULL,
                topic TEXT DEFAULT NULL,
                is_processed BOOLEAN DEFAULT 0,
                is_summarized BOOLEAN DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(channel_id, message_id)
            )
        """)

        # Автомиграция: проверяем старую БД и добавляем недостающие колонки
        cursor = await db.execute("PRAGMA table_info(messages)")
        columns = [row[1] for row in await cursor.fetchall()]
        
        if "channel_id" not in columns:
            await db.execute("ALTER TABLE messages ADD COLUMN channel_id INTEGER DEFAULT 0")
        if "message_id" not in columns:
            await db.execute("ALTER TABLE messages ADD COLUMN message_id INTEGER DEFAULT 0")
        if "media_path" not in columns:
            await db.execute("ALTER TABLE messages ADD COLUMN media_path TEXT DEFAULT NULL")
        if "is_summarized" not in columns:
            await db.execute("ALTER TABLE messages ADD COLUMN is_summarized BOOLEAN DEFAULT 0")

        await db.execute("CREATE INDEX IF NOT EXISTS idx_messages_proc ON messages(is_processed, is_summarized)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_messages_topic ON messages(topic)")
        await db.commit()
        try:
            await db.execute("ALTER TABLE messages ADD COLUMN cluster_id TEXT")
            await db.commit()
        except Exception:
            pass  # Колонка уже создана
    print(" База данных проверена и готова к работе.")

async def get_channels_list():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT channel_id, title, is_private, is_active FROM channels ORDER BY is_active DESC, title ASC") as cursor:
            return await cursor.fetchall()

async def toggle_channel_status(channel_id: int) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT is_active FROM channels WHERE channel_id = ?", (channel_id,)) as cursor:
            row = await cursor.fetchone()
            new_status = 0 if row and row[0] == 1 else 1
            await db.execute("UPDATE channels SET is_active = ? WHERE channel_id = ?", (new_status, channel_id))
            await db.commit()
            return bool(new_status)

async def get_db_stats():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM messages") as c:
            total_msgs = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM messages WHERE is_processed = 0") as c:
            unprocessed = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM channels WHERE is_active = 1") as c:
            active_channels = (await c.fetchone())[0]
        return {
            "total_messages": total_msgs,
            "unprocessed": unprocessed,
            "active_channels": active_channels
        }

async def set_message_cluster(message_id: int, cluster_id: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE messages SET cluster_id = ? WHERE id = ?",
            (cluster_id, message_id)
        )
        await db.commit()


async def mark_cluster_summarized(post_ids: list[int], cluster_id: str):
    """Помечает все посты инфоповода как суммаризированные и проставляет cluster_id."""
    if not post_ids:
        return
    placeholders = ",".join("?" for _ in post_ids)
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            f"UPDATE messages SET is_summarized = 1, cluster_id = ? WHERE id IN ({placeholders})",
            [cluster_id, *post_ids]
        )
        await db.commit()


async def get_messages_for_digest(
    db_path: str,
    limit: Optional[int] = None,
    hours: Optional[int] = None
) -> List[Dict[str, Any]]:
    """Выгружает посты из SQLite по количеству или временному окну."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        conditions = ["m.text IS NOT NULL", "length(trim(m.text)) > 25"]
        params = []

        if hours:
            since = (datetime.utcnow() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
            conditions.append("(m.created_at >= ? OR m.created_at IS NULL)")
            params.append(since)

        where_clause = " AND ".join(conditions)
        limit_clause = f"LIMIT {limit}" if limit else "LIMIT 100"

        query = f"""
            SELECT 
                m.id, m.text, m.channel_title, m.created_at,
                m.message_id, m.channel_id, c.username AS channel_username,
                m.media_path
            FROM messages m
            LEFT JOIN channels c ON m.channel_id = c.channel_id
            WHERE {where_clause}
            ORDER BY m.id DESC
            {limit_clause}
        """

        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


DEFAULT_SETTINGS = {
    "depth_type": "hours",      # 'hours' или 'count'
    "depth_val": 24,            # 12, 24, 72 или 30, 50, 100
    "only_major": 0,            # 0: все новости, 1: только крупные события (от 2 источников)
    "show_quotes": 1,           # 1: включены, 0: выключены
    "show_code": 1              # 1: включены, 0: выключены
}

async def init_settings_db(db_path: str):
    """Создает таблицу пользовательских настроек."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                depth_type TEXT DEFAULT 'hours',
                depth_val INTEGER DEFAULT 24,
                only_major BOOLEAN DEFAULT 0,
                show_quotes BOOLEAN DEFAULT 1,
                show_code BOOLEAN DEFAULT 1
            )
        """)
        await db.commit()

async def get_user_settings(db_path: str, user_id: int) -> dict:
    """Возвращает настройки пользователя или дефолтные значения."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM user_settings WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
            
            # Если пользователя еще нет, создаем дефолтную запись
            await db.execute(
                """
                INSERT OR IGNORE INTO user_settings (user_id, depth_type, depth_val, only_major, show_quotes, show_code)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, DEFAULT_SETTINGS["depth_type"], DEFAULT_SETTINGS["depth_val"], 
                 DEFAULT_SETTINGS["only_major"], DEFAULT_SETTINGS["show_quotes"], DEFAULT_SETTINGS["show_code"])
            )
            await db.commit()
            return dict(DEFAULT_SETTINGS)

async def update_user_setting(db_path: str, user_id: int, key: str, value: Any):
    """Обновляет один параметр в настройках пользователя."""
    # Гарантируем наличие записи
    await get_user_settings(db_path, user_id)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(f"UPDATE user_settings SET {key} = ? WHERE user_id = ?", (value, user_id))
        await db.commit()