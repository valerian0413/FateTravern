"""Generation trigger, model-output normalization, and intent survival across failures."""
from dataclasses import replace

import pytest

from app.application.creation import prepare
from app.application.turns import choose_tools
from app.bootstrap import build_runtime
from app.domain.types import Intent, IntentKind
from app.modules.generation import normalize_batch
from app.storage.repositories import TurnRepo, WorldRepo


def _intent(**kw):
    base = dict(kind=IntentKind.EXPLORE, target_ids=[], creation_statements=[],
                needs_clarification=False, requested_tools=[], normalized_action='前往贸易区')
    return Intent(**{**base, **kw})


def test_generation_follows_intent_gaps(world_snapshot):
    assert 'generation' in choose_tools(_intent(unresolved=['贸易区尚无地点ID']), world_snapshot)['tools']
    assert 'generation' not in choose_tools(_intent(unresolved=[]), world_snapshot)['tools']
    assert 'generation' in choose_tools(_intent(requested_tools=['generation']), world_snapshot)['tools']


def test_normalize_batch_accepts_logged_model_variants():
    batch = normalize_batch({
        'temp_entities': [
            {'temp_id': 'new:market', 'kind': 'place', 'name': '飞艇商贸区'},
            {'id': 'new:merchant', 'kind': 'character', 'name': '拾荒商贩'},
            {'id': 'new:ship', 'kind': 'object', 'name': '破旧飞艇'},
            {'id': 'new:x', 'kind': 'spaceship', 'name': '未知类型'},
        ],
        'facts': [
            {'subject_id': 'new:market', 'predicate': 'located_at', 'value': '空港东侧',
             'disclosure_rule': {'disclosure': 'public'}},
            {'subject_id': 'new:merchant', 'predicate': 'secret', 'value': '以次充好',
             'disclosure_rule': {'disclosure': 'hidden', 'requires_check': {'skill': '观察', 'difficulty': 12}}},
        ],
        'challenges': [{'id': 'new:c', 'type': 'search', 'description': '在旧货中辨认日记', 'skill': '自然知识', 'difficulty': 1}],
        'changes': [{'type': 'move', 'to': 'new:market'}],
    })
    assert [(e['id'], e['kind']) for e in batch.temp_entities] == [
        ('new:market', 'place'), ('new:merchant', 'npc'), ('new:ship', 'item'),
    ]
    assert [f['id'] for f in batch.facts] == ['new:fact-0', 'new:fact-1']
    assert batch.facts[1]['disclosure_rule']['requires_check'] is True
    challenge = batch.challenges[0]
    assert (challenge['mode'], challenge['domain'], challenge['difficulty']) == ('check', '自然知识', 'standard')
    assert challenge['goal'] == '在旧货中辨认日记'
    assert batch.changes == []


@pytest.fixture
async def failing_runtime(tmp_path, settings):
    rt = await build_runtime(
        replace(settings, db_path=tmp_path / 'gen.sqlite3', log_dir=tmp_path / 'logs'),
        require_model=False,
        fixtures={
            'intent': {'kind': 'explore', 'normalized_action': '在飞艇商贸区寻找冒险日记',
                       'requested_tools': [], 'unresolved': ['日记卖家未确定']},
            'generation': {'facts': [{'id': 'new:f', 'subject_id': 'ghost', 'predicate': 'p', 'value': 'v',
                                      'disclosure_rule': {'disclosure': 'public'}}]},
        },
    )
    yield rt
    await rt.worker.stop()
    await rt.db.close()


async def test_failed_turn_keeps_intent_for_retry(failing_runtime):
    rt = failing_runtime
    world = await prepare(rt, 'sky-islands-v1')
    await rt.chat.submit(world.id, 'req-1', '有没有冒险日记', 'auto')
    await rt.chat._tasks[(world.id, 'req-1')]

    turn = await TurnRepo(rt.db.conn).get_by_request(world.id, 'req-1')
    assert turn.stage.value == 'failed_retryable'
    assert turn.checkpoint['intent']['normalized_action'] == '在飞艇商贸区寻找冒险日记'
    assert turn.checkpoint['intent']['unresolved'] == ['日记卖家未确定']
    snapshot = await WorldRepo(rt.db.conn).get_snapshot(world.id)
    assert snapshot.world.version == world.version
