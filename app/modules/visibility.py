"""Structured disclosure grants — models cannot self-authorize visibility."""
from __future__ import annotations

from dataclasses import dataclass

from app.domain.types import DisclosureGrant, Fact, FactStatus, Knowledge


@dataclass
class DisclosureDecision:
    fact_id: str
    allowed: bool
    reason: str
    evidence_ids: list[str]


def evaluate_disclosure(rule: dict | None, action: str, scene: dict, resolution: dict | None) -> DisclosureDecision:
    fact_id = scene.get('fact_id', '')
    if not rule:
        return DisclosureDecision(fact_id, False, '无发现规则', [])
    if rule.get('disclosure') == 'public':
        return DisclosureDecision(fact_id, True, '公开事实', [])

    location_ok = True
    if rule.get('location_id') and scene.get('location_id') and rule['location_id'] != scene['location_id']:
        location_ok = False
    terms = rule.get('action_terms') or []
    terms_ok = all(isinstance(t, str) and t in action for t in terms) if terms else False
    prereq = rule.get('required_fact_ids') or []
    known = set(scene.get('known_fact_ids') or [])
    prereq_ok = all(pid in known for pid in prereq)
    success = bool(resolution and resolution.get('success'))
    needs_check = bool(rule.get('requires_check'))
    check_ok = (not needs_check) or (success and resolution.get('mode') == 'check')
    if location_ok and terms_ok and prereq_ok and check_ok and success:
        return DisclosureDecision(fact_id, True, '条件满足', list(prereq))
    return DisclosureDecision(fact_id, False, '条件未满足', [])


def issue_grants(decisions: list[DisclosureDecision], turn_id: str, observer_id: str) -> list[DisclosureGrant]:
    grants = []
    for decision in decisions:
        if decision.allowed:
            grants.append(DisclosureGrant(
                fact_id=decision.fact_id,
                observer_id=observer_id,
                turn_id=turn_id,
                evidence_ids=decision.evidence_ids,
            ))
    return grants


def filter_for(observer_id: str, facts: list[Fact] | dict, knowledge: list[Knowledge] | dict) -> list[Fact]:
    fact_map = facts if isinstance(facts, dict) else {f.id: f for f in facts}
    know_list = knowledge.values() if isinstance(knowledge, dict) else knowledge
    known_ids = {k.fact_id for k in know_list if k.observer_id == observer_id and k.fact_id}
    visible = []
    for fact in fact_map.values():
        if fact.status != FactStatus.ACTIVE:
            continue
        rule = fact.disclosure_rule or {}
        if rule.get('disclosure') == 'public' or fact.id in known_ids:
            visible.append(fact)
    return visible
