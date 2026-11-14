"""Small async SQLite helpers."""
from __future__ import annotations


async def fetchall(db, sql: str, args: tuple | list = ()):
    cursor = await db.execute(sql, args)
    try:
        return await cursor.fetchall()
    finally:
        await cursor.close()
