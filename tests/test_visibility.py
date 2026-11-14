from app.domain.types import Fact, FactStatus, Knowledge, KnowledgeBasis
from app.modules.visibility import evaluate_disclosure, filter_for, issue_grants


def test_observation_does_not_reveal_fee_without_terms():
    decision = evaluate_disclosure(
        {'action_terms': ['阅读凭据'], 'required_fact_ids': [], 'requires_check': False},
        '我看看箱子',
        {'fact_id': 'fee', 'location_id': 'port', 'known_fact_ids': []},
        {'success': True, 'mode': 'automatic'},
    )
    assert decision.allowed is False


def test_legal_discovery():
    decision = evaluate_disclosure(
        {'action_terms': ['阅读凭据'], 'required_fact_ids': ['crate-look'], 'requires_check': False},
        '我阅读凭据',
        {'fact_id': 'fee', 'location_id': 'port', 'known_fact_ids': ['crate-look']},
        {'success': True, 'mode': 'automatic'},
    )
    assert decision.allowed is True
    grants = issue_grants([decision], 'turn-1', 'player')
    assert grants[0].fact_id == 'fee'


def test_filter_hides_secret_without_knowledge():
    facts = {
        'a': Fact('a', 'w', 's', 'look', '木箱', FactStatus.ACTIVE, disclosure_rule={'disclosure': 'public'}),
        'b': Fact('b', 'w', 's', 'fee', '保管费3银', FactStatus.ACTIVE, disclosure_rule={'action_terms': ['读']}),
    }
    visible = filter_for('player', facts, {})
    assert [f.id for f in visible] == ['a']


def test_rename_does_not_bypass_knowledge():
    facts = {
        'secret': Fact('secret', 'w', 's', 'fee', '保管费', FactStatus.ACTIVE, disclosure_rule={'action_terms': ['x']}),
    }
    knowledge = {
        'k1': Knowledge('k1', 'w', 'player', KnowledgeBasis.OBSERVED, 'e1', 0, fact_id='secret'),
    }
    visible = filter_for('player', facts, knowledge)
    assert len(visible) == 1
