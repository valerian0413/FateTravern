"""Offline scenario coverage for handbook section 10 flows."""
import uuid

import pytest

from app.application.creation import prepare, start
from app.bootstrap import build_runtime
from app.config import Settings
from app.domain.errors import ModelUnavailable
from app.domain.types import Intent, IntentKind
from app.application.turns import ActionCommand
from app.providers.base import ModelResponse
from app.storage.repositories import MessageRepo, WorldRepo
from dataclasses import replace


@pytest.fixture
async def runtime(tmp_path, settings: Settings):
    rt = await build_runtime(
        replace(settings, db_path=tmp_path / 'scenario.sqlite3', log_dir=tmp_path / 'logs'),
        require_model=False,
        fixtures={
            'generation': {
                'temp_entities': [{
                    'id': 'new:airship', 'kind': 'item', 'name': '商用飞艇',
                    'attributes': {'docked': True},
                }],
                'facts': [{
                    'id': 'new:ships-visible', 'subject_id': 'new:airship',
                    'predicate': 'appearance', 'value': '停泊着两三艘商用飞艇',
                    'disclosure_rule': {'disclosure': 'public'},
                }],
                'npc_intents': [], 'challenges': [{
                    'id': 'c0', 'mode': 'automatic', 'goal': '观察飞艇',
                    'conditions': [], 'prerequisites': [], 'success_effects': [],
                    'failure_effects': [], 'duration_minutes': 1,
                }],
                'changes': [], 'unresolved_refs': [],
            },
            'narration': {
                'player_action_text': '我观察停泊的飞艇。',
                'gm_text': '空港边停着几艘商用飞艇，绳索轻轻作响。',
            },
        },
    )
    yield rt
    await rt.worker.stop()
    await rt.db.close()


async def test_sky_port_observe(runtime):
    world = await prepare(runtime, 'sky-islands-v1')
    snapshot = await WorldRepo(runtime.db.conn).get_snapshot(world.id)
    turn_id = str(uuid.uuid4())
    from app.domain.types import Turn, TurnStage
    from app.storage.repositories import TurnRepo
    from datetime import datetime, timezone
    async with runtime.db.transaction():
        await TurnRepo(runtime.db.conn).reserve(Turn(
            id=turn_id, request_id='obs-1', payload_hash='x', world_id=world.id,
            base_version=snapshot.world.version, stage=TurnStage.RECEIVED, checkpoint={},
            created_at=datetime.now(timezone.utc).isoformat(),
        ))
    result = await runtime.chat.turns.execute(ActionCommand(
        world_id=world.id, request_id='obs-1', turn_id=turn_id,
        text='我看看有哪些飞艇', intent=Intent(
            kind=IntentKind.EXPLORE, normalized_action='我看看有哪些飞艇',
            target_ids=[], creation_statements=[], needs_clarification=False, requested_tools=['generation'],
        ), base_version=snapshot.world.version,
    ))
    assert result.stage == 'completed'
    msgs = await MessageRepo(runtime.db.conn).list(world.id, channel='narrative')
    assert any('飞艇' in m['text'] for m in msgs)


async def test_opening_idempotent(runtime):
    from app.storage.repositories import EventRepo, TurnRepo

    class Model:
        async def complete(self, request):
            assert request.module == 'opening'
            return ModelResponse({
                'background': '探索者乘飞艇在浮空群岛之间往来。',
                'character': '你是刚开始冒险的小剑士。',
                'scene': '你抵达贸易据点空港，几艘商用飞艇停在栈桥旁。',
                'question': '你想先做什么？',
                'options': ['我观察停泊的飞艇', '我向商贩询问空港的情况'],
            }, '', {})

    world = await prepare(runtime, 'sky-islands-v1')
    with pytest.raises(ModelUnavailable, match='模型不可用'):
        await start(runtime, world.id, 'opening-v1')
    assert not await MessageRepo(runtime.db.conn).list(world.id)
    assert (await WorldRepo(runtime.db.conn).get(world.id)).phase.value == 'prepared'

    runtime.provider = Model()
    first = await start(runtime, world.id, 'opening-v1')
    runtime.provider = None
    second = await start(runtime, world.id, 'different-request')
    assert first.request_id == second.request_id
    db = runtime.db.conn
    turn = await TurnRepo(db).get_by_request(world.id, first.request_id)
    assert turn.result['kind'] == 'opening'
    assert len(turn.result['options']) == 2
    assert turn.checkpoint['source'] == 'system'
    messages = await MessageRepo(db).list(world.id)
    assert [(message['channel'], message['author']) for message in messages] == [
        ('narrative', 'gm'), ('interaction', 'gm'),
    ]
    assert await runtime.db.fetchall('SELECT check_id FROM checks WHERE world_id=?', (world.id,)) == []
    events = await EventRepo(db).recent(world.id)
    assert len(events) == 1 and events[0]['kind'] == 'opening'
    snapshot = await WorldRepo(db).get_snapshot(world.id)
    assert snapshot.world.clock == world.clock and snapshot.world.version == world.version
