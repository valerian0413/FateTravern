"""Regression: taking the reward note must not fail a hallucinated op_id audit."""
import json

from app.domain.changes import apply_to_copy, stabilize_changes, validate_changes
from app.modules.review import semantic_audit
from app.providers.base import ModelResponse


def test_fact_id_same_in_preview_commit_and_retry(world_snapshot):
    changes = [{'op': 'append_fact', 'op_id': 'op-fact-note-taken',
                'subject_id': world_snapshot.world.player_id, 'predicate': 'note',
                'value': '纸条已放入随身小包', 'disclosure_rule': {'disclosure': 'public'}}]
    frozen = stabilize_changes(changes, world_snapshot.world.id, 'turn-note')
    assert 'id' not in changes[0]
    assert frozen == stabilize_changes(changes, world_snapshot.world.id, 'turn-note')
    assert frozen == stabilize_changes(frozen, world_snapshot.world.id, 'turn-note')
    preview = apply_to_copy(world_snapshot, frozen, source_event_id='preview')
    committed = apply_to_copy(world_snapshot, frozen, source_event_id='committed-event')
    fact_id = frozen[0]['id']
    assert fact_id in preview.facts and fact_id in committed.facts
    assert stabilize_changes(changes, world_snapshot.world.id, 'different-turn')[0]['id'] != fact_id


def test_program_validates_actual_missing_ids_and_unknown_facts(world_snapshot):
    changes = [{'op': 'append_fact', 'subject_id': world_snapshot.world.player_id,
                'predicate': 'note', 'value': '纸条'}]
    assert any(i.path.endswith('.op_id') for i in validate_changes(changes, world_snapshot))
    changes = [{'op': 'learn', 'op_id': 'learn-missing', 'observer_id': world_snapshot.world.player_id,
                'fact_id': 'missing-fact', 'basis': 'observed'}]
    assert any(i.path.endswith('.fact_id') for i in validate_changes(changes, world_snapshot))


async def test_audit_has_structured_before_and_rejects_technical_scope(settings, world_snapshot):
    changes = stabilize_changes([{'op': 'append_fact', 'op_id': 'op-fact-note-taken',
        'subject_id': world_snapshot.world.player_id, 'predicate': 'note', 'value': '收好纸条'}],
        world_snapshot.world.id, 'test-turn')
    calls = []
    class Provider:
        async def complete(self, request):
            calls.append(request)
            if len(calls) == 1:
                payload = json.loads(request.messages[1]['content'])
                assert payload['structural_validation'] == 'passed'
                assert payload['before']['entities']
                assert payload['changes'][0]['op_id'] == 'op-fact-note-taken'
                assert payload['changes'][0]['id']
                return ModelResponse({'accepted': False, 'issues': [{
                    'category': 'structural', 'severity': 'blocking',
                    'reason': 'append_fact 操作缺少 op_id 字段'}]}, '', {})
            return ModelResponse({'accepted': True, 'issues': []}, '', {})
    issues = await semantic_audit(Provider(), settings,
        {'snapshot': world_snapshot, 'world_id': world_snapshot.world.id, 'turn_id': 'test-turn', 'public': {}},
        {'changes': changes})
    assert issues == []
    assert len(calls) == 2


async def test_semantic_conflicts_still_block(settings, world_snapshot):
    class Provider:
        async def complete(self, request):
            return ModelResponse({'accepted': False, 'issues': [{
                'category': 'disclosure', 'severity': 'blocking', 'path': 'public',
                'reason': '公开叙事泄露尚未发现的委托人秘密', 'suggested_fix': '移除秘密'}]}, '', {})
    issues = await semantic_audit(Provider(), settings,
        {'snapshot': world_snapshot, 'public': {}}, {'changes': []})
    assert issues[0].severity.value == 'blocking'
