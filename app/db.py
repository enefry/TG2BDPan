"""数据库模块：使用 aiosqlite 异步操作 SQLite，存储百度网盘 token"""

import time
from typing import Optional, Any

import aiosqlite

from config import config

_DB_PATH: str = config.DB_PATH

CREATE_TABLE_SQL: str = """
CREATE TABLE IF NOT EXISTS baidu_tokens (
    user_id     INTEGER PRIMARY KEY,
    access_token  TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    expires_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL,
    baidu_name    TEXT
);
"""


async def init_db() -> None:
    """初始化数据库，创建表结构"""
    import os

    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(CREATE_TABLE_SQL)
        try:
            await db.execute("ALTER TABLE baidu_tokens ADD COLUMN baidu_name TEXT;")
        except Exception:
            pass
        await db.commit()


async def save_token(
    user_id: int,
    access_token: str,
    refresh_token: str,
    expires_in: int,
    baidu_name: Optional[str] = None,
) -> None:
    """保存或更新某用户的 token"""
    now: int = int(time.time())
    expires_at: int = now + expires_in
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO baidu_tokens (user_id, access_token, refresh_token, expires_at, updated_at, baidu_name)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                access_token  = excluded.access_token,
                refresh_token = excluded.refresh_token,
                expires_at    = excluded.expires_at,
                updated_at    = excluded.updated_at,
                baidu_name    = COALESCE(excluded.baidu_name, baidu_name)
            """,
            (user_id, access_token, refresh_token, expires_at, now, baidu_name),
        )
        await db.commit()


async def get_token(user_id: int) -> Optional[dict[str, Any]]:
    """获取某用户的 token 记录，返回 dict 或 None"""
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM baidu_tokens WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def delete_token(user_id: int) -> None:
    """删除某用户的 token（用于重新授权）"""
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute("DELETE FROM baidu_tokens WHERE user_id = ?", (user_id,))
        await db.commit()


async def get_all_users() -> list[int]:
    """获取所有已授权的用户 ID 列表"""
    async with aiosqlite.connect(_DB_PATH) as db:
        async with db.execute("SELECT user_id FROM baidu_tokens") as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]
