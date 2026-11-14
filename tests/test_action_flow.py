"""Action planning, persisted choices, and transactional NPC cards."""
import json
from dataclasses import replace

import pytest

from app.application.creation import prepare
from app.bootstrap import build_runtime
from app.domain.errors import Conflict, ValidationError
from app.domain.types import Entity, EntityKind
from app.modules import npc, planning
from app.providers.base import ModelResponse
from app.storage.repositories import EntityRepo, MessageRepo, NpcCardRepo, TurnRepo, WorldRepo


def plan(**overrides):
    return {'challenge': {'mode': 'check', 'domain': '剑术', 'difficulty': 'easy',
                          'goal': '练习剑术', 'duration_minutes': 1, 'conditions': [],
                          'success_effects': [], 'failure_effects': []},
            'needs_choice': False, 'prompt': '', 'public_risk': '可能被守卫发现',
            'options': [], 'success_events': ['守卫点头。'], 'failure_events': ['守卫拦住了去路。'],
            **overrides}


@pytest.fixture
async def game(settings, tmp_path):
    rt = await build_runtime(replace(settings, db_path=tmp_path / 'flow.sqlite3'), require_model=False,
                             fixtures={'intent': {'kind': 'explore', 'normalized_action': '向守卫展示剑术',
                                                  'requested_tools': [], 'unresolved': []},
                                       'planning': plan(),
                                       'narration': {'player_action_text': '我展示剑术。', 'gm_text': '守卫点头。'}})
    world = await prepare(rt, 'sky-islands-v1')
    yield rt, world
    if rt.provider is not None and hasattr(rt.provider, 'close'):
        await rt.provider.close()
    await rt.worker.stop()
    await rt.db.close()


async def send(rt, wid, rid, text='向守卫展示剑术', reply_to=None):
    await rt.chat.submit(wid, rid, text, reply_to=reply_to)
    task = rt.chat._tasks.get((wid, rid))
    if task:
        await task
    return await TurnRepo(rt.db.conn).get_by_request(wid, rid)


async def count(rt, table):
    cursor = await rt.db.conn.execute('SELECT COUNT(*) FROM ' + table)
    return (await cursor.fetchone())[0]


async def test_existing_world_action_is_planned_without_generation(game):
    rt, world = game
    rt.fixtures.pop('planning')
    calls = []
    class Provider:
        async def complete(self, request):
            calls.append(request.module)
            assert request.module == 'planning'
            return ModelResponse({**plan(),'choice_timing':'none'}, '', {})
    rt.provider = Provider()
    turn = await send(rt, world.id, 'check')
    assert turn.stage.value == 'completed', turn.checkpoint
    assert calls == ['planning']  # No NPC or entity-generation model call.
    assert await count(rt, 'checks') == 1
    messages = await MessageRepo(rt.db.conn).list(world.id)
    notice = next(m for m in messages if '检定：剑术 · 简单' in m['text'])
    narrative = next(m for m in messages if m['channel'] == 'narrative')
    assert notice['sequence'] < narrative['sequence']


async def test_pause_persists_options_without_roll_or_world_write(game):
    rt, world = game
    rt.fixtures['planning'] = plan(needs_choice=True, prompt='入口挂着铜铃。', options=['继续潜入', '观察铜铃'])
    before = await WorldRepo(rt.db.conn).get_snapshot(world.id)
    turn = await send(rt, world.id, 'pause')
    assert turn.result['kind'] == 'decision'
    assert await count(rt, 'checks') == 0
    after = await WorldRepo(rt.db.conn).get_snapshot(world.id)
    assert (after.world.version, after.world.clock) == (before.world.version, before.world.clock)
    assert not await MessageRepo(rt.db.conn).list(world.id, 'narrative')
    # A fresh query, as after browser reload, restores actionable choices.
    rows = await MessageRepo(rt.db.conn).list(world.id, 'interaction')
    assert rows[-1]['options'] == ['继续潜入', '观察铜铃']
    assert rows[-1]['choices_active']
    rt.fixtures['planning'] = plan()
    result = await send(rt, world.id, 'chosen', text='继续潜入', reply_to='pause')
    assert result.stage.value == 'completed', result.checkpoint
    assert await count(rt, 'checks') == 1
    await send(rt, world.id, 'chosen', text='继续潜入', reply_to='pause')
    assert await count(rt, 'checks') == 1
    with pytest.raises(Conflict):
        await send(rt, world.id, 'double', text='观察铜铃', reply_to='pause')


async def test_npc_important_update_commits_with_turn(game, monkeypatch):
    rt, world = game
    monkeypatch.setattr('app.rules.four_dice.roll', lambda: (1, 1, 1, 1))
    entity = Entity(id='guard', world_id=world.id, kind=EntityKind.NPC, name='守卫',
                    attributes={'condition': '正常', 'goals': ['看守港口']}, revision=1)
    async with rt.db.transaction():
        await EntityRepo(rt.db.conn).upsert(entity)
        await NpcCardRepo(rt.db.conn).upsert(world.id, npc.build_card(
            {'id': entity.id, 'name': entity.name, 'attributes': entity.attributes}))
    rt.fixtures['planning']['challenge']['success_effects'] = [
        {'op': 'update_npc_card', 'op_id': 'guard-trust', 'entity_id': 'guard',
         'expected': {'current_activity': '待命'}, 'patch': {'current_activity': '指导玩家剑术'},
         'reason': '玩家展示剑术后，守卫同意提供指导'}]
    turn = await send(rt, world.id, 'npc-update')
    assert turn.stage.value == 'completed', turn.checkpoint
    snapshot = await WorldRepo(rt.db.conn).get_snapshot(world.id)
    assert snapshot.npc_cards['guard'].current_activity == '指导玩家剑术'
    assert snapshot.npc_cards['guard'].goals == ['看守港口']


async def test_failed_commit_keeps_npc_and_world_unchanged(game, monkeypatch):
    rt, world = game
    monkeypatch.setattr('app.rules.four_dice.roll', lambda: (1, 1, 1, 1))
    async with rt.db.transaction():
        await EntityRepo(rt.db.conn).upsert(Entity(id='guard', world_id=world.id, kind=EntityKind.NPC,
                                                  name='守卫', attributes={}, revision=1))
    rt.fixtures['planning']['challenge']['success_effects'] = [
        {'op': 'update_npc_card', 'op_id': 'npc', 'entity_id': 'guard', 'expected': {'condition': '正常'},
         'patch': {'condition': '受伤'}, 'reason': '训练中受伤'},
        {'op': 'resource_delta', 'op_id': 'bad', 'key': 'nonexistent', 'expected_value': 0, 'delta': -1}]
    turn = await send(rt, world.id, 'bad')
    assert turn.stage.value == 'failed_retryable'
    snapshot = await WorldRepo(rt.db.conn).get_snapshot(world.id)
    assert snapshot.world.version == world.version
    assert 'guard' not in snapshot.npc_cards
    assert not await MessageRepo(rt.db.conn).list(world.id, 'narrative')


async def test_retry_reuses_frozen_plan_and_dice(game):
    rt, world = game
    rt.fixtures.pop('narration')
    rt.fixtures.pop('planning')
    calls = []
    class Provider:
        async def complete(self, request):
            calls.append(request.module)
            if request.module == 'planning':
                return ModelResponse({**plan(),'choice_timing':'none'}, '', {})
            raise RuntimeError('narration unavailable')
    rt.provider = Provider()
    turn = await send(rt, world.id, 'retry')
    assert turn.stage.value == 'failed_retryable'
    assert await count(rt, 'checks') == 1
    rt.fixtures['narration'] = {'player_action_text': '我展示剑术。', 'gm_text': '训练结束。'}
    await rt.chat.turns.resume(turn.id, world.id)
    assert calls.count('planning') == 1
    assert await count(rt, 'checks') == 1
    rows = await MessageRepo(rt.db.conn).list(world.id, 'interaction')
    assert len([m for m in rows if '检定：' in m['text']]) == 1


async def test_post_action_choices_persist(game):
    rt, world = game
    rt.fixtures['planning'] = plan(needs_choice=True, choice_timing='after_action',
                                   prompt='接下来呢？', options=['询问守卫', '查看港口'])
    rt.fixtures['narration'].update(stop_reason='new_choice', choice_prompt='叙事不应决定下一步',
                                    options=['绕开守卫', '追向港口'])
    turn = await send(rt, world.id, 'next')
    assert turn.result['stop_reason'] == 'new_choice'
    rows = await MessageRepo(rt.db.conn).list(world.id, 'interaction')
    assert rows[-1]['options'] == ['询问守卫', '查看港口']
    assert rows[-1]['choices_active']


def test_missing_challenge_never_defaults_success():
    with pytest.raises(ValidationError):
        planning.validate_plan({}, {})


@pytest.mark.parametrize('options', [[], ['继续'], ['a'] * 3, ['a', 'b', 'c', 'd', 'e']])
def test_choice_validation(options):
    with pytest.raises(ValidationError):
        planning.validate_options(options, required=True)


async def test_invalid_player_resource_attribute_is_repaired_before_roll(game):
    rt, world = game
    rt.fixtures.pop('planning')
    attempts = []
    class Provider:
        async def complete(self, request):
            attempts.append(request)
            result = plan()
            if len(attempts) == 1:
                result['challenge']['success_effects'] = [{
                    'op': 'set_attribute', 'op_id': 'bad-resource', 'entity_id': world.player_id,
                    'key': 'stress', 'expected': 3, 'value': 2}]
            return ModelResponse({**result,'choice_timing':'none'}, '', {})
    rt.provider = Provider()
    turn = await send(rt, world.id, 'repair')
    assert turn.stage.value == 'completed', turn.checkpoint
    assert len(attempts) == 2
    assert 'repair' in attempts[1].messages[-1]['content']
    assert await count(rt, 'checks') == 1
    snapshot = await WorldRepo(rt.db.conn).get_snapshot(world.id)
    assert snapshot.entities[world.player_id].attributes['resources']['stress'] == 3


async def test_invalid_failure_branch_rejected_even_if_success_likely(game):
    rt, world = game
    rt.fixtures['planning']['challenge']['failure_effects'] = [
        {'op': 'set_attribute', 'op_id': 'bad', 'entity_id': 'missing', 'key': 'x', 'value': 1}]
    turn = await send(rt, world.id, 'bad-branch')
    assert turn.stage.value == 'failed_retryable'
    assert await count(rt, 'checks') == 0


async def test_free_input_continues_pending_candidates(game):
    rt, world = game
    rt.fixtures['intent']['requested_tools'] = ['generation']
    rt.fixtures['generation'] = {'temp_entities': [
        {'id': 'new:bell', 'kind': 'item', 'name': '铜铃', 'attributes': {}}]}
    rt.fixtures['planning'] = plan(needs_choice=True, prompt='入口有铜铃。', options=['观察铜铃', '绕开入口'])
    paused = await send(rt, world.id, 'pause-free')
    bell_id = paused.checkpoint['prepared']['registered']['temp_entities'][0]['id']
    snapshot = await WorldRepo(rt.db.conn).get_snapshot(world.id)
    assert bell_id not in snapshot.entities
    rt.fixtures['intent']['requested_tools'] = []
    rt.fixtures['planning'] = plan()
    result = await send(rt, world.id, 'free', text='我先询问旁边的人。')
    assert result.stage.value == 'completed', result.checkpoint
    snapshot = await WorldRepo(rt.db.conn).get_snapshot(world.id)
    assert snapshot.entities[bell_id].name == '铜铃'


async def test_narrator_cannot_add_choices_to_a_completed_plan(game):
    rt, world = game
    rt.fixtures['narration'].update(stop_reason='new_choice', choice_prompt='擅自添加的选项',
                                    options=['询问守卫', '查看港口'])
    turn = await send(rt, world.id, 'narrator-options')
    assert turn.result['stop_reason'] == 'action_complete'
    assert turn.result['options'] == []
    assert turn.result['prompt'] == ''


async def test_new_commission_fact_can_be_learned_in_the_same_turn(game):
    rt, world = game
    rt.fixtures['planning']['challenge'].update(mode='automatic', success_effects=[
        {'op': 'append_fact', 'op_id': 'fact-commission', 'subject_id': world.player_id,
         'predicate': 'commission_offer', 'value': '鲁恩委托调查断桅岩。',
         'disclosure_rule': {'disclosure': 'public'}},
        {'op': 'learn', 'op_id': 'learn-commission', 'observer_id': world.player_id,
         'fact_id': 'fact-commission', 'basis': 'observed'},
    ])
    turn = await send(rt, world.id, 'learn-new-commission')
    assert turn.stage.value == 'completed', turn.checkpoint
    snapshot = await WorldRepo(rt.db.conn).get_snapshot(world.id)
    fact = next(f for f in snapshot.facts.values() if f.predicate == 'commission_offer')
    assert any(k.fact_id == fact.id and k.observer_id == world.player_id for k in snapshot.knowledge.values())


@pytest.mark.parametrize('retreat', [False, True])
async def test_opposed_attack_waits_for_player_and_preserves_dice(game, monkeypatch, retreat):
    from app.rules.character import CONFIG

    rt, world = game
    db = rt.db.conn
    await db.execute("UPDATE worlds SET ruleset_id='fate-lite-v1',ruleset_config=? WHERE id=?",
                     (json.dumps(CONFIG), world.id))
    snapshot = await WorldRepo(db).get_snapshot(world.id)
    player = snapshot.entities[world.player_id]
    player.attributes['stress_tracks'] = {'unified': [False] * 3}
    player.attributes['consequences'] = {
        'mild': {'capacity': 2, 'text': None}, 'moderate': {'capacity': 4, 'text': None},
        'severe': {'capacity': 6, 'text': None},
    }
    await EntityRepo(db).upsert(player)
    await EntityRepo(db).upsert(Entity(id='guard', world_id=world.id, kind=EntityKind.NPC, name='守卫',
                                     attributes={'skills': {'战斗': 3}}, revision=1))
    await db.commit()
    rt.fixtures['planning']['challenge'].update(
        mode='opposed', domain='战斗',
        combat={'attacker_id': 'guard', 'defender_id': player.id, 'defense_domain': '运动', 'injury': '擦伤'},
    )
    rolls = iter([(0, 0, 0, 0), (0, 0, 0, 0)])
    monkeypatch.setattr('app.rules.four_dice.roll', lambda: next(rolls))
    first = await send(rt, world.id, 'first-attack')
    assert first.result['combat_decision']['kind'] == 'before_roll'
    assert await count(rt, 'checks') == 0
    with pytest.raises(Conflict, match='处理当前攻击'):
        await rt.chat.submit(world.id, 'skip-attack', '我去买东西')
    second = await send(rt, world.id, 'response', first.result['options'][int(retreat)], 'first-attack')
    assert second.stage.value == 'completed', second.checkpoint
    if retreat:
        assert await count(rt, 'checks') == 0
    else:
        assert second.result['combat_decision']['resolution']['damage'] == 3
        third = await send(rt, world.id, 'absorb', second.result['options'][0], 'response')
        assert third.stage.value == 'completed', third.checkpoint
        assert await count(rt, 'checks') == 1
    state = (await WorldRepo(db).get_snapshot(world.id)).entities[player.id].attributes
    if retreat:
        assert state['combat_status'] == 'concede' and state['fate_points'] == 1
    else:
        assert state['stress_tracks']['unified'] == [False, False, True]
