"""Native encounter routing, persistent round state, and damage pauses."""
import json

import pytest

from app.application import demos
from app.application.chat import ChatService
from app.bootstrap import build_runtime, shutdown
from app.domain.errors import Conflict, ValidationError
from app.providers.base import ModelResponse
from app.storage.repositories import CheckRepo, EncounterRepo, TurnRepo, WorldRepo


def skill_call(request, checks):
    assert 'submit_encounter_checks' in {tool['function']['name'] for tool in request.tools}
    return ModelResponse(None, '', {}, message={'tool_calls': [{
        'id': request.call_id, 'type': 'function',
        'function': {'name': 'submit_encounter_checks', 'arguments': json.dumps({'checks': checks})},
    }]})


class EncounterModel:
    def __init__(self, kind='combat', count=1):
        self.kind = kind
        self.count = count
        self.round_requests = 0
        self.narration_requests = []

    async def complete(self, request):
        payload = json.loads(request.messages[-1]['content'])
        if request.module == 'narration':
            self.narration_requests.append(payload)
            return ModelResponse({'player_action_text': payload['public_outcome']['authorized_action'][:50],
                                  'gm_text': '甲板在风中轻晃，眼前的交锋暂告一段落。'}, '', {})
        if request.module == 'host_tools':
            names = {tool['function']['name'] for tool in request.tools}
            assert {'start_encounter', 'play_encounter_turn'} <= names
            text = payload['text']
            if text == '这是什么地方？':
                name, args = 'reply_to_host', {'reply': '这是当前遭遇场景。'}
            elif not payload['public'].get('encounter'):
                name, args = 'start_encounter', {'kind': self.kind, 'action': text}
            else:
                name, args = 'play_encounter_turn', {'action': text}
            return ModelResponse(None, '', {}, message={'role': 'assistant', 'tool_calls': [{
                'id': request.call_id, 'type': 'function',
                'function': {'name': name, 'arguments': json.dumps(args)},
            }]})
        if request.module == 'encounter_setup':
            return ModelResponse({
                'applicable': True, 'title': '风浪中的对抗', 'scene': '风吹过甲板。',
                'goal': '保住飞艇', 'opponent_goal': '逼退玩家',
                'opponents': [
                    {'name': f'雷暴云鲨 {i}', 'aspect': '鳞背带电', 'habit': '伺机扑击',
                     'skills': {}, 'distance': 'close'}
                    for i in range(self.count)
                ],
            }, '', {})
        if request.module == 'encounter_skills':
            checks = {}
            for check_id, action in payload['pending_checks'].items():
                check = {'skill': '工匠' if action['kind'] == 'prepare' else '运动',
                         'reason': '按当前动作和场景选择技能'}
                if action['mode'] == 'opposed':
                    check['defense_skill'] = '运动'
                if action['requires_reach']:
                    check['reach'] = 'close'
                checks[check_id] = check
            return skill_call(request, checks)
        assert request.module == 'encounter_round'
        self.round_requests += 1
        text = payload['player_text']
        if text == '组装陷阱':
            move = {'kind': 'prepare', 'rounds': 2, 'skill': '工匠', 'description': '组装陷阱'}
        elif text == '继续当前准备':
            move = {'kind': 'continue'}
        elif text == '撤离':
            move = {'kind': 'withdraw'}
        else:
            move = {'kind': 'advance' if self.kind != 'combat' else 'wait', 'description': text}
        response = 'attack' if text == '等待云鲨攻来' else 'guard' if self.kind == 'combat' else 'wait'
        return ModelResponse({
            'player': move,
            'opponents': {unit['id']: {'kind': response, 'description': '扑向舷边'}
                          for unit in payload['participants'][1:]},
        }, '', {})

    async def close(self):
        pass


@pytest.fixture
async def encounter_game(settings):
    runtime = await build_runtime(settings, require_model=False)
    world = await demos.play(runtime, 'xuan-yun-hai')
    yield runtime, world
    await shutdown(runtime)


async def play(runtime, world, rid, text, **kwargs):
    await runtime.chat.submit(world.id, rid, text, **kwargs)
    task = runtime.chat._tasks.get((world.id, rid))
    if task:
        await task
    return await TurnRepo(runtime.db.conn).get_by_request(world.id, rid)


async def test_native_encounter_two_round_preparation_survives_reload(encounter_game, monkeypatch):
    runtime, world = encounter_game
    runtime.provider = EncounterModel()
    monkeypatch.setattr('app.rules.four_dice.roll', lambda: (1, 1, 1, 1))
    opening = await play(runtime, world, 'open', '组装陷阱')
    assert opening.result['encounter']['elapsed_seconds'] == 8
    assert opening.result['encounter']['pending']['remaining'] == 1
    assert opening.result['options'][0] == '继续当前准备'
    await runtime.chat.close()
    runtime.chat = ChatService(runtime)

    consultation = await play(runtime, world, 'question', '这是什么地方？')
    assert consultation.result['kind'] == 'discussion'
    assert (await WorldRepo(runtime.db.conn).get(world.id)).version == 1
    second = await play(runtime, world, 'second', '继续当前准备', reply_to='open')
    assert second.result['encounter']['elapsed_seconds'] == 16
    assert second.result['encounter']['pending'] is None
    assert second.result['encounter']['prepared'] == '组装陷阱'
    assert (await CheckRepo(runtime.db.conn).get(world.id, f'{world.id}:second:0'))['dice'] == [1] * 4

    # A request with the same ID is idempotent, even after reloading the chat service.
    same = await play(runtime, world, 'second', '继续当前准备', reply_to='open')
    assert same.result == second.result
    assert (await WorldRepo(runtime.db.conn).get(world.id)).version == 2


async def test_incoming_hit_resumes_next_unit_without_rolling_again(encounter_game, monkeypatch):
    runtime, world = encounter_game
    runtime.provider = model = EncounterModel(count=2)
    dice = iter([(1, 1, 0, 0), (0, 0, 0, 0), (-1, 0, 0, 0), (0, 0, 0, 0)])
    monkeypatch.setattr('app.rules.four_dice.roll', lambda: next(dice))
    turn = await play(runtime, world, 'hit', '等待云鲨攻来')
    assert turn.result['combat_decision']['kind'] == 'encounter_hit'
    assert turn.result['encounter']['elapsed_seconds'] == 0
    assert turn.result['options'][0] == '承受2 点压力格'
    with pytest.raises(Conflict):
        await runtime.chat.submit(world.id, 'skip', '我继续攻击')
    scar_option = next(option for option in turn.result['options'] if '伤痕' in option)
    with pytest.raises(ValidationError, match='仍能行动'):
        await runtime.chat.submit(world.id, 'invalid-scar', scar_option,
                                  reply_to='hit', combat_description='昏迷')

    await runtime.chat.close()
    runtime.chat = ChatService(runtime)
    followup = await play(runtime, world, 'absorb', turn.result['options'][0], reply_to='hit')
    assert followup.result['encounter']['elapsed_seconds'] == 8
    assert followup.result['encounter']['round'] == 2
    assert 'combat_decision' not in followup.result
    assert model.round_requests == 1
    assert (await CheckRepo(runtime.db.conn).get(world.id, f'{world.id}:hit:0'))['payload']['damage'] == 2
    assert (await CheckRepo(runtime.db.conn).get(world.id, f'{world.id}:absorb:0'))['payload']['damage'] == 0
    snapshot = await WorldRepo(runtime.db.conn).get_snapshot(world.id)
    assert snapshot.entities[world.player_id].attributes['stress_tracks']['unified'] == [False, True, False]


async def test_cloud_shark_targets_ship_without_injuring_player(encounter_game, monkeypatch):
    runtime, world = encounter_game
    snapshot = await WorldRepo(runtime.db.conn).get_snapshot(world.id)
    ship_id = snapshot.entities[world.player_id].attributes['location_id']
    setup_id = None

    class CloudShark(EncounterModel):
        async def complete(self, request):
            nonlocal setup_id
            if request.module == 'encounter_setup':
                setup_id = request.turn_id
                result = await super().complete(request)
                result.content['opponent_goal'] = '撞坏飞艇外板'
                result.content['scene_items'] = ['备用鱼叉']
                result.content['opponents'][0].update(name='云鲨', entity_id=ship_id)
                return result
            if request.module == 'encounter_round':
                payload = json.loads(request.messages[-1]['content'])
                shark = payload['participants'][1]
                assert shark['id'] != ship_id
                return ModelResponse({
                    'player': {'kind': 'observe', 'description': '寻找鱼叉发射器'},
                    'opponents': {shark['id']: {
                        'kind': 'attack', 'target_id': 'objective', 'description': '摆尾撞向飞艇外板',
                    }},
                }, '', {})
            return await super().complete(request)

    runtime.provider = CloudShark()
    dice = iter([(1, 1, 0, 0), (0, 0, 0, 0)] * 2)
    monkeypatch.setattr('app.rules.four_dice.roll', lambda: next(dice))

    first = await play(runtime, world, 'observe', '我想找找有没有鱼叉发射器')
    assert first.id == setup_id and first.result['encounter']['round'] == 2
    assert first.result['encounter']['prepared'] is None
    assert first.result['encounter']['progress'] == {'player': 0, 'opponents': 2}
    assert 'combat_decision' not in first.result
    events = first.result['public_outcome']['observable_events']
    assert any(event['text'] == '你能看到：备用鱼叉。' for event in events)
    assert any('查看手边' in option for option in first.result['options'])

    second = await play(runtime, world, 'observe-again', '我再查看手边')
    assert second.result['encounter']['result'] == 'opponents_goal'
    snapshot = await WorldRepo(runtime.db.conn).get_snapshot(world.id)
    assert snapshot.entities[world.player_id].attributes['stress_tracks']['unified'] == [False] * 3
    assert any(fact.predicate == 'encounter_outcome' and fact.value['achieved'] == '撞坏飞艇外板'
               for fact in snapshot.facts.values())


async def test_skill_agent_handles_study_and_ranged_spell(encounter_game, monkeypatch):
    runtime, world = encounter_game
    focus = '老师留下的旧法器（施法媒介，完好可用）'

    class SpellEncounter(EncounterModel):
        async def complete(self, request):
            if request.module == 'encounter_setup':
                result = await super().complete(request)
                result.content['opponents'][0]['distance'] = 'far'
                return result
            payload = json.loads(request.messages[-1]['content'])
            if request.module == 'encounter_round':
                shark = payload['participants'][1]['id']
                if payload['encounter']['round'] == 1:
                    player = {'kind': 'study', 'description': '仔细察看云鲨翻身时的鳞甲',
                              'target_id': shark, 'weakness': '翻身时露出鳃缝'}
                else:
                    player = {'kind': 'attack', 'description': '用旧法器施法攻击鳃缝',
                              'target_id': shark, 'skill': '战斗', 'use_preparation': True}
                return ModelResponse({'player': player, 'opponents': {shark: {'kind': 'guard'}}}, '', {})
            if request.module == 'encounter_skills':
                check_id, = payload['pending_checks']
                action = payload['pending_checks'][check_id]
                assert '学术' in payload['skill_catalog'] and 'skill' not in action
                assert payload['player_training']['skill_reason']
                if action['kind'] == 'study':
                    if not payload.get('correction'):
                        return skill_call(request, [])
                    return skill_call(request, {check_id: {'skill': '调查', 'reason': '细查鳞甲寻破绽'}})
                assert focus in payload['inventory']
                if payload['encounter']['round'] == 3:
                    assert payload['participants'][0]['skill_history'][-1]['skill'] == '学术'
                return skill_call(request, {check_id: {
                    'skill': '学术', 'defense_skill': '运动',
                    'reach': 'far', 'equipment': focus, 'reason': '用受过训练的法术引动旧法器',
                }})
            return await super().complete(request)

    runtime.provider = SpellEncounter()
    dice = iter([(1, 0, 0, 0), (0, 0, 0, 0), (1, 1, 0, 0),
                 (0, 0, 0, 0), (1, 1, 0, 0)])
    monkeypatch.setattr('app.rules.four_dice.roll', lambda: next(dice))
    repo = CheckRepo(runtime.db.conn)

    study = await play(runtime, world, 'study', '尝试观察云鲨的弱点，用调动魔力攻击它的弱点')
    assert study.result['encounter']['prepared'] == '翻身时露出鳃缝'
    check = (await repo.get(world.id, f'{world.id}:study:0'))['payload']
    assert check['domain'] == '调查' and check['mode'] == 'check'
    assert not await repo.get(world.id, f'{world.id}:study:1')

    for request_id in ('cast', 'cast-again'):
        cast = await play(runtime, world, request_id, '用旧法器施法攻击它的鳃缝')
        assert cast.result['encounter']['prepared'] is None
        check = (await repo.get(world.id, f'{world.id}:{request_id}:0'))['payload']
        assert check['domain'] == '学术' and check['mode'] == 'opposed'
        assert check['skill_bonus'] == 2
        assert sum(modifier['value'] for modifier in check['modifiers']) == (1 if request_id == 'cast' else 0)


@pytest.mark.parametrize('search_result', [True, False, None], ids=['found', 'missed', 'quick-glance'])
async def test_search_and_defense_resolve_in_the_same_round(encounter_game, monkeypatch, search_result):
    runtime, world = encounter_game
    searching = search_result is not None

    class SearchEncounter(EncounterModel):
        async def complete(self, request):
            payload = json.loads(request.messages[-1]['content'])
            if request.module == 'encounter_setup':
                result = await super().complete(request)
                result.content['scene_items'] = ['缆绳', '鱼篓']
                return result
            if request.module == 'encounter_round':
                shark = payload['participants'][1]['id']
                player = {'kind': 'search' if searching else 'observe', 'description': '查看附近可用的物品'}
                if searching:
                    player['search_items'] = ['缆绳']
                return ModelResponse({'player': player, 'opponents': {
                    shark: {'kind': 'attack', 'description': '云鲨张口扑咬'},
                }}, '', {})
            if request.module == 'encounter_skills':
                pending = payload['pending_checks']
                tool = next(tool for tool in request.tools if tool['function']['name'] == 'submit_encounter_checks')
                schema = tool['function']['parameters']['properties']['checks']
                assert set(schema['required']) == set(pending)
                assert [action['kind'] for action in pending.values()] == (['search', 'attack'] if searching else ['attack'])
                attack_id = next(key for key, action in pending.items() if action['kind'] == 'attack')
                assert pending[attack_id]['defender_id'] == world.player_id
                checks = {attack_id: {'skill': '战斗', 'defense_skill': '运动', 'reach': 'close', 'reason': '扑咬与闪避'}}
                if searching:
                    checks['check_1'] = {'skill': '调查', 'reason': '在扑咬前辨认可用物件'}
                else:
                    assert 'player_text' not in payload
                    if 'correction' not in payload:
                        # Reproduce the extra player entry from the reported failed request.
                        return skill_call(request, {**checks, 'player': {'skill': '调查', 'reason': '扫视甲板'}})
                    assert '恰好为 check_1' in payload['correction'] and 'defense_skill' in payload['correction']
                return skill_call(request, checks)
            return await super().complete(request)

    runtime.provider = SearchEncounter()
    dice = [(1, 0, 0, 0) if search_result else (0, 0, 0, 0)] if searching else []
    dice = iter([*dice, (1, 1, 0, 0), (0, 0, 0, 0)])
    rolls = []

    def roll():
        value = next(dice)
        rolls.append(value)
        return value

    monkeypatch.setattr('app.rules.four_dice.roll', roll)
    turn = await play(runtime, world, 'scan', '我查看手边有哪些用得上的东西')
    assert turn.result['combat_decision']['kind'] == 'encounter_hit'
    assert turn.result['encounter']['elapsed_seconds'] == 0
    events = turn.result['public_outcome']['observable_events']
    if searching:
        check = await CheckRepo(runtime.db.conn).get(world.id, f'{world.id}:scan:0')
        assert check['payload']['purpose'] == 'search' and check['payload']['success'] == search_result
        assert check['payload']['domain'] == '调查'
        assert any('物品：缆绳' in event['text'] for event in events) == search_result
    else:
        assert any('你能看到：缆绳、鱼篓' in event['text'] for event in events)
    attack_index = int(searching)
    attack = await CheckRepo(runtime.db.conn).get(world.id, f'{world.id}:scan:{attack_index}')
    assert attack['payload']['defense_domain'] == '运动'
    assert attack['payload']['damage'] == 2

    resumed = await play(runtime, world, 'absorb', turn.result['options'][0], reply_to='scan')
    assert resumed.result['encounter']['round'] == 2
    assert resumed.result['encounter']['elapsed_seconds'] == 8
    assert len(rolls) == 2 + int(searching)


@pytest.mark.parametrize('kind', ['chase', 'negotiation'])
async def test_noncombat_encounter_finishes_at_goal(encounter_game, monkeypatch, kind):
    runtime, world = encounter_game
    runtime.provider = EncounterModel(kind)
    dice = iter([(1, 1, 1, 1), (0, 0, 0, 0)] * 2)
    monkeypatch.setattr('app.rules.four_dice.roll', lambda: next(dice))
    first = await play(runtime, world, 'first', '我推进眼前的目标')
    assert first.result['encounter']['progress']['player'] == 2
    second = await play(runtime, world, 'second', '我继续争取优势')
    assert second.result['encounter']['result'] == 'player_goal'
    assert second.result['encounter']['elapsed_seconds'] == 16
    saved = await EncounterRepo(runtime.db.conn).get(world.id)
    assert saved['result'] == 'player_goal'
    snapshot = await WorldRepo(runtime.db.conn).get_snapshot(world.id)
    assert snapshot.entities[world.player_id].attributes['stress_tracks']['unified'] == [False] * 3


async def test_encounter_retries_commit_with_saved_rolls(encounter_game, monkeypatch):
    from app.application import encounters, recovery

    runtime, world = encounter_game
    runtime.provider = model = EncounterModel('chase')
    dice = iter([(1, 1, 1, 1), (0, 0, 0, 0)])
    monkeypatch.setattr('app.rules.four_dice.roll', lambda: next(dice))
    original = encounters.commit_turn

    async def fail_after_writing(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError('模拟写入失败')

    monkeypatch.setattr(encounters, 'commit_turn', fail_after_writing)
    failed = await play(runtime, world, 'retry', '我追上去')
    assert failed.stage.value == 'failed_retryable'
    assert (await WorldRepo(runtime.db.conn).get(world.id)).version == 0
    assert await EncounterRepo(runtime.db.conn).get(world.id) is None
    assert failed.checkpoint['encounter_result']['checks'][0]['dice'] == [1] * 4

    monkeypatch.setattr(encounters, 'commit_turn', original)
    await recovery.retry(runtime, world.id, 'retry')
    await runtime.chat._tasks[(world.id, 'retry')]
    result = await TurnRepo(runtime.db.conn).get_by_request(world.id, 'retry')
    assert result.result['encounter']['progress']['player'] == 2
    assert model.round_requests == 1
    assert len(model.narration_requests) == 1


@pytest.mark.parametrize('prepared', [True, False])
async def test_compound_action_uses_distinct_skills_and_skips_failed_dependency(encounter_game, monkeypatch, prepared):
    runtime, world = encounter_game

    class CompoundEncounter(EncounterModel):
        async def complete(self, request):
            payload = json.loads(request.messages[-1]['content'])
            if request.module == 'encounter_round':
                shark = payload['participants'][1]['id']
                return ModelResponse({'player': [
                    {'kind': 'prepare', 'description': '抛出鱼饵引开注意', 'rounds': 1, 'seconds': 3},
                    {'kind': 'attack', 'description': '趁机用旧法器施法攻击', 'target_id': shark,
                     'seconds': 5, 'requires_previous_success': True, 'use_preparation': True},
                ], 'opponents': {shark: {'kind': 'guard'}}}, '', {})
            if request.module == 'encounter_skills':
                assert [row['kind'] for row in payload['pending_checks'].values()] == ['prepare', 'attack']
                return skill_call(request, {
                    'check_1': {'skill': '欺瞒', 'reason': '用鱼饵制造假目标'},
                    'check_2': {'skill': '学术', 'reason': '以研习的法术攻击',
                                'defense_skill': '运动', 'reach': 'far',
                                'equipment': '老师留下的旧法器（施法媒介，完好可用）'},
                })
            return await super().complete(request)

    runtime.provider = model = CompoundEncounter()
    dice = iter([(1, 1, 0, 0) if prepared else (-1, -1, 0, 0),
                 (0, 0, 0, 0), (0, 0, 0, 0)])
    monkeypatch.setattr('app.rules.four_dice.roll', lambda: next(dice))
    turn = await play(runtime, world, 'compound', '抛鱼饵引开注意，再用旧法器施法攻击')
    assert turn.stage.value == 'completed'
    assert turn.result['encounter']['elapsed_seconds'] == 8
    repo = CheckRepo(runtime.db.conn)
    first = (await repo.get(world.id, f'{world.id}:compound:0'))['payload']
    assert first['domain'] == '欺瞒' and first['success'] == prepared
    second = await repo.get(world.id, f'{world.id}:compound:1')
    if prepared:
        assert second['payload']['domain'] == '学术'
        assert sum(row['value'] for row in second['payload']['modifiers']) == 1
    else:
        assert second is None
        assert any('未执行后续动作' in event['text'] for event in turn.result['public_outcome']['observable_events'])
    assert not await repo.get(world.id, f'{world.id}:compound:2')
    narrative = next(message for message in turn.result['messages'] if message['author'] == 'gm' and message['channel'] == 'narrative')
    assert narrative['text'] == '甲板在风中轻晃，眼前的交锋暂告一段落。'
    payload, = model.narration_requests
    assert len(payload['style']['resolved_checks']) == (2 if prepared else 1)
    assert payload['public_outcome']['observable_events'] == turn.result['public_outcome']['observable_events']


def test_compound_plan_respects_time_attack_limit_and_long_preparation():
    from app.application.encounters import plan_round

    state = {'order': ['player', 'shark'], 'kind': 'combat', 'scene_items': [],
             'units': {'player': {'status': 'active'}, 'shark': {'status': 'active'}}}
    for steps in (
        [{'kind': 'study', 'target_id': 'shark', 'weakness': '鳃缝', 'seconds': 5},
         {'kind': 'attack', 'target_id': 'shark', 'seconds': 5}],
        [{'kind': 'attack', 'target_id': 'shark', 'seconds': 4}] * 2,
        [{'kind': 'prepare', 'rounds': 2, 'seconds': 3},
         {'kind': 'attack', 'target_id': 'shark', 'seconds': 5}],
    ):
        with pytest.raises(ValidationError):
            plan_round({'player': steps, 'opponents': {'shark': {'kind': 'wait'}}}, state)
    plan = plan_round({'player': {'kind': 'advance', 'target_id': 'objective'},
                       'opponents': {'shark': {'kind': 'wait'}}}, state)
    assert plan['player']['target_id'] == 'shark'


async def test_encounter_narration_failure_retries_without_replanning_or_rerolling(encounter_game, monkeypatch):
    from app.application import recovery

    runtime, world = encounter_game

    class InterruptedNarrator(EncounterModel):
        fail = True

        async def complete(self, request):
            if request.module == 'narration' and self.fail:
                self.fail = False
                raise RuntimeError('叙事连接中断')
            return await super().complete(request)

    runtime.provider = model = InterruptedNarrator('chase')
    dice = iter([(1, 1, 1, 1), (0, 0, 0, 0)])
    monkeypatch.setattr('app.rules.four_dice.roll', lambda: next(dice))
    failed = await play(runtime, world, 'narration-retry', '追上对手')
    assert failed.stage.value == 'failed_retryable'
    assert failed.checkpoint['encounter_result']['checks'][0]['dice'] == [1] * 4
    assert (await WorldRepo(runtime.db.conn).get(world.id)).version == 0
    await recovery.retry(runtime, world.id, 'narration-retry')
    await runtime.chat._tasks[(world.id, 'narration-retry')]
    completed = await TurnRepo(runtime.db.conn).get_by_request(world.id, 'narration-retry')
    assert completed.stage.value == 'completed'
    assert completed.result['encounter']['progress']['player'] == 2
    assert model.round_requests == 1


async def test_narration_receives_pending_damage_without_uncommitted_wound(encounter_game, monkeypatch):
    runtime, world = encounter_game
    runtime.provider = model = EncounterModel()
    dice = iter([(1, 1, 0, 0), (0, 0, 0, 0)])
    monkeypatch.setattr('app.rules.four_dice.roll', lambda: next(dice))
    turn = await play(runtime, world, 'incoming', '等待云鲨攻来')
    assert turn.result['combat_decision']['kind'] == 'encounter_hit'
    payload, = model.narration_requests
    assert payload['style']['pending_damage']['target_id'] == world.player_id
    assert payload['style']['pending_damage']['damage'] == 2
    assert not any('留下伤痕' in event['text'] for event in payload['public_outcome']['observable_events'])
