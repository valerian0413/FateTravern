"""The shipped 悬云海 demo copies a fresh, playable archive."""
import pytest

from app.application import demos, workshop
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
    assert demo['name'] == '悬云海-预设'
    assert demo['character_name'] == '云隼·见习学者'
    assert '云鲨' in demo['blurb']

    first = await demos.play(runtime, 'xuan-yun-hai')
    second = await demos.play(runtime, 'xuan-yun-hai')
    assert first.id != second.id
    assert first.name == demo['name'] and first.phase.value == 'prepared'

    snapshot = await WorldRepo(runtime.db.conn).get_snapshot(first.id)
    player = snapshot.entities[snapshot.world.player_id]
    assert player.name == demo['character_name']
    assert 'coins' not in player.attributes['resources']
    assert any(entity.name == '铜币' and entity.attributes == {
        'owner': player.id, 'quantity': 5, 'currency': True,
    } for entity in snapshot.entities.values())
    assert player.attributes['skills']['学术'] == 2
    assert '云鲨' in player.attributes['opening_intent']
    names = {entity.name for entity in snapshot.entities.values()}
    assert {'雷云层中的飞艇', '冒险者协会', '记录本与炭笔'} <= names
    place = next(entity for entity in snapshot.entities.values() if entity.kind == EntityKind.PLACE)
    assert player.attributes['location_id'] == place.id
    assert any(fact.predicate == 'scene' and fact.subject_id == place.id for fact in snapshot.facts.values())

    preset = await workshop.create(runtime, {'kind': 'preset', 'id': 'xuan-yun-hai-v1'})
    assert preset['book']['name'] == '悬云海'
    confirmed = await workshop.confirm(runtime, preset['id'], {'revision': 0, 'action': 'confirm_world'})
    demo_book = snapshot.world.ruleset_config['world_book']
    assert confirmed['book']['sections'] == demo_book['sections']
    assert confirmed['book']['initial_scene'] == demo_book['initial_scene']

    with pytest.raises(NotFound):
        await demos.play(runtime, 'missing')
