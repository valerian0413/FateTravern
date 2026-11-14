"""Unique entry for new entity candidates."""
from __future__ import annotations

import logging
import uuid

from app.domain.types import CHANGE_OPS, CandidateBatch, ChallengeMode, EntityKind
from app.prompts.loader import load
from app.providers.base import ModelRequest

log = logging.getLogger('modules.generation')

BATCH_KEYS = ('temp_entities', 'facts', 'npc_intents', 'challenges', 'changes', 'unresolved_refs')
KIND_ALIASES = {
    'character': 'npc', 'person': 'npc', 'creature': 'npc',
    'location': 'place', 'area': 'place', 'district': 'place',
    'object': 'item', 'thing': 'item',
}
DIFFICULTIES = ('easy', 'standard', 'hard', 'extreme')


async def propose(provider, settings, request: dict) -> CandidateBatch:
    if provider is None or request.get('force_batch') is not None:
        data = request.get('force_batch') or {}
        return normalize_batch(data)

    spec = load('generation', settings.prompt_version, settings.prompts_dir)
    response = await provider.complete(ModelRequest(
        module='generation',
        output_budget=4096,
        prompt_version=settings.prompt_version,
        schema_version='1',
        world_id=request.get('world_id'),
        turn_id=request.get('turn_id'),
        call_id=uuid.uuid4().hex,
        messages=[
            {'role': 'system', 'content': spec.text + '\n只返回 JSON。'},
            {'role': 'user', 'content': str(request)},
        ],
    ))
    return normalize_batch(response.content)


def normalize_batch(data: dict) -> CandidateBatch:
    """Coerce recognizable model variants into engine schema; drop what cannot be used."""
    raw = {k: list(data.get(k) or []) for k in BATCH_KEYS}
    dropped: list[str] = []

    entities = []
    for ent in raw['temp_entities']:
        if not isinstance(ent, dict):
            continue
        ent = dict(ent)
        if 'id' not in ent and 'temp_id' in ent:
            ent['id'] = ent.pop('temp_id')
        kind = str(ent.get('kind') or '').lower()
        kind = KIND_ALIASES.get(kind, kind)
        if kind not in {k.value for k in EntityKind} or kind == EntityKind.PLAYER.value:
            dropped.append(f"entity {ent.get('name')}: kind={ent.get('kind')}")
            continue
        ent['kind'] = kind
        entities.append(ent)

    facts = []
    for index, fact in enumerate(raw['facts']):
        if not isinstance(fact, dict) or not fact.get('subject_id') or 'value' not in fact:
            dropped.append(f'fact[{index}]: 缺少 subject_id 或 value')
            continue
        fact = dict(fact)
        if not fact.get('id'):
            fact['id'] = fact.pop('temp_id', None) or f'new:fact-{index}'
        rule = fact.get('disclosure_rule')
        if isinstance(rule, dict) and isinstance(rule.get('requires_check'), dict):
            fact['disclosure_rule'] = {**rule, 'requires_check': True}
        facts.append(fact)

    challenges = []
    for challenge in raw['challenges']:
        normalized = _normalize_challenge(challenge, dropped)
        if normalized is None:
            dropped.append(f'challenge: {challenge}')
            continue
        challenges.append(normalized)

    changes = _valid_ops(raw['changes'], 'gen', dropped)

    if dropped:
        log.warning('generation candidates dropped: %s', dropped)
    return CandidateBatch(
        temp_entities=entities,
        facts=facts,
        npc_intents=raw['npc_intents'],
        challenges=challenges,
        changes=changes,
        unresolved_refs=raw['unresolved_refs'],
    )


def _valid_ops(items, prefix: str, dropped: list[str]) -> list[dict]:
    ops = []
    for item in items or []:
        if not isinstance(item, dict) or item.get('op') not in CHANGE_OPS:
            dropped.append(f'{prefix}: {item}')
            continue
        ops.append({'op_id': f"{prefix}-{item['op']}-{len(ops)}", **item})
    return ops


def _minutes(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 1


def _normalize_challenge(challenge, dropped: list[str]) -> dict | None:
    if not isinstance(challenge, dict):
        return None
    domain = challenge.get('domain') or challenge.get('skill')
    difficulty = challenge.get('difficulty')
    mode = challenge.get('mode')
    if mode is None:
        mode = ChallengeMode.CHECK.value if (domain or difficulty is not None) else ChallengeMode.AUTOMATIC.value
    if mode not in {m.value for m in ChallengeMode}:
        return None
    if mode == ChallengeMode.CHECK.value and difficulty not in DIFFICULTIES:
        difficulty = 'standard'
    conditions = [
        c for c in (challenge.get('conditions') or [])
        if isinstance(c, dict) and c.get('source_id') and c.get('direction') in ('advantage', 'disadvantage')
    ]
    return {
        'id': challenge.get('id') or 'challenge-0',
        'mode': mode,
        'goal': challenge.get('goal') or challenge.get('description') or '',
        'domain': domain if mode == ChallengeMode.CHECK.value else None,
        'difficulty': difficulty if mode == ChallengeMode.CHECK.value else None,
        'conditions': conditions,
        'prerequisites': list(challenge.get('prerequisites') or []),
        'success_effects': _valid_ops(challenge.get('success_effects'), 'success', dropped),
        'failure_effects': _valid_ops(challenge.get('failure_effects'), 'failure', dropped),
        'duration_minutes': _minutes(challenge.get('duration_minutes') or 1),
    }
