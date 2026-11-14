"""Native encounter routing, persistent round state, and damage pauses."""
import json

import pytest

from app.application import demos
from app.application.chat import ChatService
from app.bootstrap import build_runtime, shutdown
from app.domain.errors import Conflict
from app.providers.base import ModelResponse
from app.storage.repositories import CheckRepo, EncounterRepo, TurnRepo, WorldRepo


class EncounterModel:
    def __init__(self, kind='combat', count=1):
        self.kind = kind
        self.count = count
        self.round_requests = 0

    async def complete(self, request):
        payload = json.loads(request.messages[-1]['content'])
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
