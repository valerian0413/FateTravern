"""Whitelisted context builders — never hand full world dict to every module."""
from __future__ import annotations

from app.domain.types import DisclosureGrant, PublicOutcome, Snapshot
from app.modules.visibility import filter_for


async def build_intent_context(snapshot: Snapshot, text: str) -> dict:
    player_id = snapshot.world.player_id
    visible_facts = filter_for(player_id, snapshot.facts, snapshot.knowledge)
    places = [e for e in snapshot.entities.values() if e.kind.value == 'place']
    return {
        'world_id': snapshot.world.id,
        'public': {
            'setting': snapshot.world.setting,
            'world_book': public_book(snapshot),
            'clock': snapshot.world.clock,
            'phase': snapshot.world.phase.value,
            'visible_facts': [{'id': f.id, 'subject_id':f.subject_id, 'predicate': f.predicate, 'value': f.value} for f in visible_facts],
            'entities': [{'id':e.id,'name':e.name,'kind':e.kind.value,'aliases':e.attributes.get('aliases',[])} for e in snapshot.entities.values() if e.id==player_id or e.id in {f.subject_id for f in visible_facts}],
            'places': [{'id': p.id, 'name': p.name} for p in places],
            'player_skills': snapshot.entities[player_id].attributes.get('skills', {}),
        },
        'text': text,
    }


async def build_world_context(snapshot: Snapshot, ids: list[str], budget: int = 20) -> dict:
    entities = []
    for eid in ids[:budget]:
        entity = snapshot.entities.get(eid)
        if entity:
            entities.append({
                'id': entity.id, 'kind': entity.kind.value, 'name': entity.name,
                'attributes': entity.attributes,
            })
    facts = []
    priority = {eid:i for i,eid in enumerate(ids)}
    for fact in sorted(snapshot.facts.values(), key=lambda f:priority.get(f.subject_id,len(ids))):
        if fact.subject_id in ids[:budget] or not ids:
            facts.append({
                'id': fact.id, 'subject_id': fact.subject_id, 'predicate': fact.predicate,
                'value': fact.value, 'disclosure_rule': fact.disclosure_rule,
            })
        if len(facts) >= budget:
            break
    return {
        'world_id': snapshot.world.id,
        'version': snapshot.world.version,
        'world_book': snapshot.world.ruleset_config.get('world_book'),
        'setting': snapshot.world.setting,
        'entities': entities,
        'facts': facts[:budget],
        'npc_cards': [
            {'entity_id': c.entity_id, 'identity': c.identity, 'location_id': c.location_id}
            for eid, c in snapshot.npc_cards.items() if not ids or eid in ids
        ][:budget],
    }


def build_public_context(snapshot: Snapshot, grants: list[DisclosureGrant], events: list[dict],
                         authorized_action: str, stop_reason: str) -> PublicOutcome:
    granted_ids = {g.fact_id for g in grants}
    visible_facts = []
    for fact in snapshot.facts.values():
        rule = fact.disclosure_rule or {}
        if rule.get('disclosure') == 'public' or fact.id in granted_ids:
            visible_facts.append({
                'id': fact.id, 'subject_id': fact.subject_id, 'predicate': fact.predicate,
                'value': fact.value,
            })
    # Also include already-known facts via knowledge.
    known = {k.fact_id for k in snapshot.knowledge.values() if k.observer_id == snapshot.world.player_id}
    for fid in known:
        fact = snapshot.facts.get(fid)
        if fact and not any(v['id'] == fid for v in visible_facts):
            visible_facts.append({
                'id': fact.id, 'subject_id': fact.subject_id, 'predicate': fact.predicate, 'value': fact.value,
            })
    visible_entities = []
    for entity in snapshot.entities.values():
        if entity.kind.value == 'player' or entity.id in {f['subject_id'] for f in visible_facts}:
            visible_entities.append({'id': entity.id, 'kind': entity.kind.value, 'name': entity.name})
    return PublicOutcome(
        authorized_action=authorized_action,
        observable_events=events,
        visible_entities=visible_entities,
        visible_facts=visible_facts,
        stop_reason=stop_reason,
    )


def public_book(snapshot):
    book = snapshot.world.ruleset_config.get('world_book')
    if not book: return None
    return {'name':book['name'], 'sections': {key:[e for e in entries if e['visibility']=='public' and e['status']!='open']
                                             for key,entries in book['sections'].items()}}
