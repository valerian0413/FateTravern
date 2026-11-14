from app.domain.types import Summary
from app.modules.memory import validate_summary


def test_entity_ids_not_fact_ids():
    summary = Summary(
        id='s1', world_id='w', event_ids=[], entity_ids=['fact-1'], audience_id='p',
        text='x', covered_until_sequence=0,
    )
    issues = validate_summary(summary, valid_entity_ids={'ent-1'}, valid_fact_ids={'fact-1'})
    assert issues
