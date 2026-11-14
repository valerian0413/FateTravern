"""Game time advances by actions only — never by wall clock."""
from __future__ import annotations

from app.domain.types import Snapshot


def due_events(snapshot: Snapshot, target_time: int) -> list:
    return [
        e for e in snapshot.scheduled.values()
        if e.status == 'pending' and snapshot.world.clock < e.due_at <= target_time
    ]
