"""Outbox worker for summarize/index jobs."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.modules import memory
from app.storage.repositories import EventRepo, JobRepo, SummaryRepo, WorldRepo

log = logging.getLogger('jobs.worker')


def retry_at(attempt: int) -> str:
    delay = min(300, 2 ** attempt)
    return (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()


class Worker:
    def __init__(self, runtime):
        self.runtime = runtime
        self._task: asyncio.Task | None = None
        self._stopping = False

    def start(self) -> None:
        if self._task is None:
            self._stopping = False
            self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def run(self) -> None:
        while not self._stopping:
            try:
                jobs = await JobRepo(self.runtime.db.conn).claim(limit=5)
                for job in jobs:
                    try:
                        await self.dispatch(job)
                        await JobRepo(self.runtime.db.conn).complete(job['id'])
                        await self.runtime.db.conn.commit()
                    except Exception:
                        log.exception('job failed %s', job.get('dedup_key'))
                        if job['attempts'] >= 5:
                            await JobRepo(self.runtime.db.conn).complete(job['id'])
                        else:
                            await JobRepo(self.runtime.db.conn).fail(job['id'], retry_at(job['attempts']))
                        await self.runtime.db.conn.commit()
            except Exception:
                log.exception('worker loop error')
            await asyncio.sleep(1)

    async def dispatch(self, job: dict) -> None:
        if job['job_type'] == 'summarize':
            world_id = job['world_id']
            events = await EventRepo(self.runtime.db.conn).recent(world_id, limit=5)
            snapshot = await WorldRepo(self.runtime.db.conn).get_snapshot(world_id)
            summary = await memory.summarize(
                self.runtime.provider, self.runtime.settings, events,
                valid_entity_ids=list(snapshot.entities),
                audience=snapshot.world.player_id,
                world_id=world_id,
            )
            issues = memory.validate_summary(
                summary, set(snapshot.entities), set(snapshot.facts),
            )
            if issues:
                raise RuntimeError(issues[0].reason)
            # The adventure may have been deleted while the model was summarizing.
            async with self.runtime.chat.turns.lock_for(world_id):
                if not await self.runtime.db.fetchall('SELECT 1 FROM worlds WHERE id=?', (world_id,)):
                    return
                await SummaryRepo(self.runtime.db.conn).insert(world_id, {
                    'id': summary.id,
                    'event_ids': summary.event_ids,
                    'entity_ids': summary.entity_ids,
                    'audience_id': summary.audience_id,
                    'text': summary.text,
                    'covered_until_sequence': summary.covered_until_sequence,
                })
            return
        raise RuntimeError(f'unknown job type {job["job_type"]}')
