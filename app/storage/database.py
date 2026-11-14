"""SQLite connection, migrations, and transaction helpers."""
from __future__ import annotations

import asyncio
import shutil
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

MIGRATIONS_DIR = Path(__file__).parent / 'migrations'


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None
        self._transaction_lock = asyncio.Lock()

    async def connect(self) -> aiosqlite.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute('PRAGMA foreign_keys = ON')
        await self._conn.execute('PRAGMA busy_timeout = 5000')
        await self._conn.execute('PRAGMA journal_mode = WAL')
        return self._conn

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError('数据库未连接')
        return self._conn

    async def fetchall(self, sql: str, args: tuple = ()) -> list:
        cursor = await self.conn.execute(sql, args)
        rows = await cursor.fetchall()
        await cursor.close()
        return list(rows)

    async def migrate(self) -> None:
        await self.conn.execute(
            'CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)'
        )
        await self.conn.commit()
        rows = await self.fetchall('SELECT version FROM schema_migrations ORDER BY version')
        applied = {r[0] for r in rows}
        scripts = sorted(MIGRATIONS_DIR.glob('*.sql'))
        for script in scripts:
            version = int(script.name.split('_', 1)[0])
            if version in applied:
                continue
            if self.path.exists() and self.path.stat().st_size > 0:
                backup = self.path.with_suffix(self.path.suffix + f'.bak-{version}')
                if not backup.exists():
                    shutil.copy2(self.path, backup)
            sql = script.read_text(encoding='utf-8')
            await self.conn.executescript(sql)
            await self.conn.execute(
                'INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)',
                (version, datetime.now(timezone.utc).isoformat()),
            )
            await self.conn.commit()

    @asynccontextmanager
    async def transaction(self):
        async with self._transaction_lock:
            try:
                yield self.conn
                await self.conn.commit()
            except BaseException:
                await self.conn.rollback()
                raise

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
