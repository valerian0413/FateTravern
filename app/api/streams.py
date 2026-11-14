"""Persisted stream events with SSE resume, plus ephemeral model text."""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

from app.storage.repositories import StreamRepo


class _Waiter:
    def __init__(self):
        self.event = asyncio.Event()
        self.live: list[dict] = []


class StreamHub:
    def __init__(self, runtime):
        self.runtime = runtime
        self._waiters: dict[str, list[_Waiter]] = {}

    def notify(self, world_id: str) -> None:
        for waiter in self._waiters.get(world_id, []):
            waiter.event.set()

    def publish(self, key: str, event: dict) -> None:
        for waiter in self._waiters.get(key, []):
            waiter.live.append(event)
            waiter.event.set()

    def _attach(self, key: str) -> _Waiter:
        waiter = _Waiter()
        self._waiters.setdefault(key, []).append(waiter)
        return waiter

    def _detach(self, key: str, waiter: _Waiter) -> None:
        waiters = self._waiters.get(key, [])
        if waiter in waiters:
            waiters.remove(waiter)

    async def subscribe(self, world_id: str, after_sequence: int = 0) -> AsyncIterator[dict]:
        last = after_sequence
        waiter = self._attach(world_id)
        try:
            while True:
                rows = await StreamRepo(self.runtime.db.conn).after(world_id, last)
                for row in rows:
                    last = row['id']
                    yield row
                while waiter.live:
                    yield waiter.live.pop(0)
                try:
                    await asyncio.wait_for(waiter.event.wait(), timeout=15)
                except asyncio.TimeoutError:
                    yield {'id': last, 'type': 'heartbeat', 'world_id': world_id, 'payload': {}}
                waiter.event.clear()
        finally:
            self._detach(world_id, waiter)

    async def listen(self, key: str) -> AsyncIterator[dict]:
        """Ephemeral events only. Used where there is no persisted world log."""
        waiter = self._attach(key)
        try:
            while True:
                while waiter.live:
                    yield waiter.live.pop(0)
                try:
                    await asyncio.wait_for(waiter.event.wait(), timeout=15)
                except asyncio.TimeoutError:
                    yield {'type': 'heartbeat', 'payload': {}}
                waiter.event.clear()
        finally:
            self._detach(key, waiter)
