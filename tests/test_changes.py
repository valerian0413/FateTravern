"""Change registration, preconditions, cross-world rejection, rollback via validate."""
from __future__ import annotations

import pytest

from app.domain.changes import apply_to_copy, register, validate_changes
from app.domain.errors import ValidationError
from app.domain.types import CandidateBatch, EntityKind, FactStatus


def test_register_temp_ids(world_snapshot):
    batch = CandidateBatch(
        temp_entities=[{'id': 'new:boat', 'kind': 'item', 'name': '飞艇'}],
        facts=[{'id': 'new:f1', 'subject_id': 'new:boat', 'predicate': 'docked', 'value': True}],
        npc_intents=[], challenges=[], changes=[], unresolved_refs=[],
    )
    registered = register(batch, world_snapshot)
    assert not registered['temp_entities'][0]['id'].startswith('new:')
    assert registered['facts'][0]['subject_id'] == registered['temp_entities'][0]['id']


def test_resource_delta_rejects_negative(world_snapshot):
    changes = [{
        'op': 'resource_delta', 'op_id': 'r1', 'key': 'coins',
        'expected_value': 5, 'delta': -6,
    }]
    issues = validate_changes(changes, world_snapshot)
    assert any(i.path == 'changes[0]' for i in issues)


def test_apply_append_fact(world_snapshot):
    place = next(e for e in world_snapshot.entities.values() if e.kind == EntityKind.PLACE)
    changes = [{
        'op': 'append_fact', 'op_id': 'a1', 'id': 'fact-1',
        'subject_id': place.id, 'predicate': 'appearance', 'value': '停泊着飞艇',
    }]
    next_state = apply_to_copy(world_snapshot, changes, source_event_id='evt-1')
    assert 'fact-1' in next_state.facts
    assert next_state.facts['fact-1'].status == FactStatus.ACTIVE


def test_cross_world_create_rejected(world_snapshot):
    with pytest.raises(ValidationError):
        apply_to_copy(world_snapshot, [{
            'op': 'create_entity', 'op_id': 'c1', 'id': 'aaaa', 'kind': 'npc',
            'name': '外人', 'world_id': 'other-world', 'attributes': {},
        }])


def test_set_attribute_precondition(world_snapshot):
    place = next(e for e in world_snapshot.entities.values() if e.kind == EntityKind.PLACE)
    with pytest.raises(ValidationError):
        apply_to_copy(world_snapshot, [{
            'op': 'set_attribute', 'op_id': 's1', 'entity_id': place.id,
            'key': 'description', 'expected': '错的', 'value': '新描述',
            'expected_revision': place.revision,
        }])
