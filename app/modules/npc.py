"""NPC cards: local lookup and validated updates."""
from __future__ import annotations

from dataclasses import asdict

from app.domain.errors import ValidationError
from app.domain.types import NpcCard


def build_card(candidate: dict) -> NpcCard:
    candidate = {**(candidate.get("attributes") or {}), **candidate}
    return NpcCard(
        entity_id=candidate['id'],
        identity=candidate.get('identity') or candidate.get('profile') or candidate.get('name', ''),
        traits=list(candidate.get('traits') or []),
        goals=list(candidate.get('goals') or []),
        boundaries=list(candidate.get('boundaries') or []),
        current_activity=candidate.get('current_activity') or '待命',
        location_id=candidate.get('location_id'),
        condition=candidate.get('condition') or '正常',
    )


CARD_FIELDS = {'identity': str, 'traits': list, 'goals': list, 'boundaries': list,
               'current_activity': str, 'location_id': (str, type(None)), 'condition': str}


def related_cards(snapshot, target_ids, text):
    """Local lookup only; no model call and no unrelated world-wide NPC enumeration."""
    player = snapshot.entities[snapshot.world.player_id]
    location = player.attributes.get('location_id')
    cards = []
    for entity in snapshot.entities.values():
        if entity.kind.value != 'npc':
            continue
        card = snapshot.npc_cards.get(entity.id) or build_card(
            {'id': entity.id, 'name': entity.name, 'attributes': entity.attributes})
        if not (entity.id in target_ids or entity.name in text or (location and card.location_id == location)):
            continue
        knowledge = [k for k in snapshot.knowledge.values() if k.observer_id == entity.id]
        cards.append({'entity_id': entity.id, 'name': entity.name, 'card': asdict(card),
                      'knowledge': [{'fact_id': k.fact_id, 'belief_text': k.belief_text,
                                     'basis': k.basis.value,
                                     'value': snapshot.facts[k.fact_id].value if k.fact_id in snapshot.facts else None}
                                    for k in knowledge],
                      'relations': [asdict(r) for r in snapshot.relations.values()
                                    if entity.id in (r.source_entity_id, r.target_entity_id)]})
    return cards


def validate_update(change, card, entities):
    patch, expected = change.get('patch'), change.get('expected')
    if not isinstance(patch, dict) or not patch or not isinstance(expected, dict) or set(expected) != set(patch):
        raise ValidationError('角色卡变更必须逐字段给出 expected 和 patch', path='npc_card')
    if not isinstance(change.get('reason'), str) or not change['reason'].strip():
        raise ValidationError('角色卡变更需要因果依据', path='npc_card.reason')
    for key, value in patch.items():
        if key not in CARD_FIELDS or not isinstance(value, CARD_FIELDS[key]):
            raise ValidationError('非法角色卡字段或值', path='npc_card.' + key)
        if isinstance(value, list) and any(not isinstance(v, str) for v in value):
            raise ValidationError('角色卡列表必须包含文本', path='npc_card.' + key)
        if getattr(card, key) != expected[key]:
            raise ValidationError('角色卡前置条件不满足', path='npc_card.' + key)
        if key == 'location_id' and value is not None and (value not in entities or entities[value].kind.value != 'place'):
            raise ValidationError('角色位置必须引用已有地点', path='npc_card.location_id')
