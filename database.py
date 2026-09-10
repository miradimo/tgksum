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