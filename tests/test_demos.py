"""The shipped 悬云海 demo copies a fresh, playable archive."""
import pytest

from app.application import demos
from app.bootstrap import build_runtime
from app.domain.errors import NotFound
from app.domain.types import EntityKind
from app.storage.repositories import WorldRepo


@pytest.fixture
async def runtime(settings):
    rt = await build_runtime(settings, require_model=False)
    await rt.worker.stop()
    yield rt
    await rt.db.close()


async def test_xuan_yun_hai_demo_copies_a_fresh_archive(runtime):
    listed = demos.list_demos(runtime)
    demo = next(item for item in listed if item['id'] == 'xuan-yun-hai')
    assert demo['name'] == '悬云海'
    assert demo['character_name'] == '未署名的见习学者'
    assert '风栈岛' in demo['blurb']

    first = await demos.play(runtime, 'xuan-yun-hai')
    second = await demos.play(runtime, 'xuan-yun-hai')
    assert first.id != second.id
    assert first.name == '悬云海' and first.phase.value == 'prepared'

    snapshot = await WorldRepo(runtime.db.conn).get_snapshot(first.id)
    player = snapshot.entities[snapshot.world.player_id]
    assert player.name == '未署名的见习学者'
    assert player.attributes['resources']['coins'] == 50
    assert player.attributes['skills']['学术'] == 2
    assert '风栈岛' in player.attributes['opening_intent']
    assert snapshot.world.ruleset_config['world_book']['revision'] == 2
    names = {entity.name for entity in snapshot.entities.values()}
    assert {'风栈岛', '栈道会', '风栈酒馆', '空白笔记与炭笔'} <= names
    place = next(entity for entity in snapshot.entities.values() if entity.kind == EntityKind.PLACE)
    assert player.attributes['location_id'] == place.id
    assert any(fact.predicate == 'scene' and fact.subject_id == place.id for fact in snapshot.facts.values())

    with pytest.raises(NotFound):
        await demos.play(runtime, 'missing')
