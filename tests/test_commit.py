import uuid

import pytest

from app.application.commit import commit_turn, validate
from app.domain.types import PublicOutcome, Turn, TurnDraft, TurnStage
from app.storage.repositories import MessageRepo, TurnRepo, WorldRepo


@pytest.fixture
async def prepared_turn(db, world_snapshot):
    turn = Turn(
        id=str(uuid.uuid4()),
        request_id='req-1',
        payload_hash='abc',
        world_id=world_snapshot.world.id,
        base_version=0,
        stage=TurnStage.COMMITTING,
        checkpoint={},
    )
    async with db.transaction():
        await TurnRepo(db.conn).reserve(turn)
    return turn


async def test_commit_idempotent_messages(db, world_snapshot, prepared_turn):
    place = next(e for e in world_snapshot.entities.values() if e.kind.value == 'place')
    draft = TurnDraft(
        base_version=0,
        registered_candidates={},
        resolution_ids=[],
        changes=[{
            'op': 'append_fact', 'op_id': 'f1', 'id': str(uuid.uuid4()),
            'subject_id': place.id, 'predicate': 'ships', 'value': '可见几艘飞艇',
            'disclosure_rule': {'disclosure': 'public'},
        }],
        events=[],
        public_outcome=PublicOutcome('我观察飞艇', [], [], [], 'action_complete'),
        narration={'player_action_text': '我观察停泊的飞艇。', 'gm_text': '空港停着几艘商用飞艇，帆布在风里轻响。'},
        next_jobs=[],
    )
    assert not validate(draft, world_snapshot)
    async with db.transaction():
        committed = await commit_turn(
            db.conn, world_id=world_snapshot.world.id, turn_id=prepared_turn.id,
            request_id='req-1', draft=draft, elapsed_minutes=1,
        )
    assert len(committed.messages) == 2
    snap = await WorldRepo(db.conn).get_snapshot(world_snapshot.world.id)
    assert snap.world.version == 1
    assert snap.world.clock == 1
    msgs = await MessageRepo(db.conn).list(world_snapshot.world.id, channel='narrative')
    assert len(msgs) == 2


async def test_version_conflict(db, world_snapshot, prepared_turn):
    draft = TurnDraft(
        base_version=99,
        registered_candidates={},
        resolution_ids=[],
        changes=[],
        events=[],
        public_outcome=PublicOutcome('x', [], [], [], 'action_complete'),
        narration={'player_action_text': '我站着。', 'gm_text': '什么也没发生。'},
        next_jobs=[],
    )
    with pytest.raises(Exception):
        async with db.transaction():
            await commit_turn(
                db.conn, world_id=world_snapshot.world.id, turn_id=prepared_turn.id,
                request_id='req-1', draft=draft,
            )
