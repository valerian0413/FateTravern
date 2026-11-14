"""Delete a single adventure and its dependent records."""
import asyncio
import json
import logging

from app.application.memory_search import MemoryIndex
from app.domain.errors import Conflict
from app.storage.repositories import WorldRepo

logger = logging.getLogger(__name__)

# NPC cards reference entities. The other records are scoped by world_id.
WORLD_RECORDS = (
    'npc_cards', 'summary_entities', 'outbox', 'stream_events', 'scheduled_events',
    'summaries', 'events', 'messages', 'proposals', 'checks', 'turns', 'relations',
    'knowledge', 'facts', 'entities',
)


async def delete_archive(runtime, world_id: str) -> None:
    if runtime.chat.has_active_request(world_id):
        raise Conflict('主持人正在处理这个存档，请等回复结束后再删除。')
    db = runtime.db.conn
    async with runtime.chat.turns.lock_for(world_id):
        if runtime.chat.has_active_request(world_id):
            raise Conflict('主持人正在处理这个存档，请等回复结束后再删除。')
        async with runtime.db.transaction():
            world = await WorldRepo(db).get(world_id)
            linked = await runtime.db.fetchall(
                "SELECT id, revision, payload FROM workshop WHERE json_extract(payload, '$.world_id')=?",
                (world_id,),
            )
            for row in linked:
                data = json.loads(row['payload'])
                revision = row['revision'] + 1
                preserved = {
                    'id': data['id'], 'revision': revision, 'stage': 'saved', 'book': data['book'],
                    'world_id': None, 'character': None, 'history': [], 'pending': None, 'options': [],
                }
                await db.execute(
                    "UPDATE workshop SET revision=?,stage='saved',payload=? WHERE id=?",
                    (revision, json.dumps(preserved, ensure_ascii=False), row['id']),
                )
            for table in WORLD_RECORDS:
                await db.execute(f'DELETE FROM {table} WHERE world_id=?', (world_id,))
            await db.execute('DELETE FROM worlds WHERE id=?', (world_id,))

        # The index is a cache and does not own any save data.
        try:
            index = runtime.memory_index
            path = runtime.settings.db_path.with_suffix('.vectors')
            if index is None and path.is_dir():
                index = MemoryIndex(path, runtime.settings.memory_model_dir)
            if index is not None:
                await asyncio.to_thread(index.delete_world, world_id, world.player_id)
        except Exception:
            logger.exception('无法清除已删除存档 %s 的向量缓存', world_id)
