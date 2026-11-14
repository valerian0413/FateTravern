from types import SimpleNamespace

from app.application.memory_search import search_memory
from app.domain.types import Channel
from app.storage.repositories import MessageRepo


async def test_keyword_retrieval_checks_all_committed_history(db, world_snapshot):
    runtime = SimpleNamespace(db=db, memory_index=None)
    repo = MessageRepo(runtime.db.conn)
    world = world_snapshot.world
    await repo.append(world.id, Channel.NARRATIVE, 'gm', '风栈岛上有五片区域。')
    for _ in range(201):
        await repo.append(world.id, Channel.NARRATIVE, 'gm', '空气中弥漫着潮气。')
    await repo.append(world.id, Channel.INTERACTION, 'gm', '未确认线索只在交谈里。')
    await runtime.db.conn.commit()
    result = await search_memory(runtime, world.id, '风栈岛 区域')
    assert result['matches'][0]['text'] == '风栈岛上有五片区域。'
    assert not (await search_memory(runtime, world.id, '未确认线索'))['matches']
