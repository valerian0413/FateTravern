"""Memory summaries with audience isolation."""
from __future__ import annotations

import uuid

from app.domain.types import Issue, IssueSeverity, Summary
from app.prompts.loader import load
from app.providers.base import ModelRequest


def validate_summary(summary: Summary | dict, valid_entity_ids: set[str], valid_fact_ids: set[str]) -> list[Issue]:
    data = summary if isinstance(summary, dict) else {
        'entity_ids': summary.entity_ids, 'text': summary.text,
    }
    issues = []
    for eid in data.get('entity_ids') or []:
        if eid in valid_fact_ids:
            issues.append(Issue(IssueSeverity.BLOCKING, 'entity_ids', '事实ID不可作为实体ID', '移除事实ID'))
        elif eid not in valid_entity_ids:
            issues.append(Issue(IssueSeverity.BLOCKING, 'entity_ids', f'未知实体 {eid}', '仅使用 valid_entity_ids'))
    return issues


async def summarize(provider, settings, committed_events: list[dict], valid_entity_ids: list[str],
                    audience: str, world_id: str) -> Summary:
    if provider is None:
        text = '；'.join(e.get('player_visible_summary', '') for e in committed_events if e.get('player_visible_summary'))[:100]
        entity_ids = [eid for eid in valid_entity_ids if any(eid in str(e) for e in committed_events)][:5]
        return Summary(
            id=str(uuid.uuid4()), world_id=world_id, event_ids=[e['id'] for e in committed_events],
            entity_ids=entity_ids, audience_id=audience, text=text or '（无摘要）',
            covered_until_sequence=max((e.get('sequence') or 0) for e in committed_events) if committed_events else 0,
        )
    spec = load('memory', settings.prompt_version, settings.prompts_dir)
    response = await provider.complete(ModelRequest(
        module='memory',
        prompt_version=settings.prompt_version,
        schema_version='1',
        world_id=world_id,
        turn_id=None,
        call_id=uuid.uuid4().hex,
        messages=[
            {'role': 'system', 'content': spec.text + '\n只返回 JSON。'},
            {'role': 'user', 'content': str({
                'events': committed_events, 'valid_entity_ids': valid_entity_ids, 'audience': audience,
            })},
        ],
    ))
    data = response.content
    return Summary(
        id=str(uuid.uuid4()), world_id=world_id,
        event_ids=[e['id'] for e in committed_events],
        entity_ids=list(data.get('entity_ids') or []),
        audience_id=audience,
        text=data.get('text') or '',
        covered_until_sequence=max((e.get('sequence') or 0) for e in committed_events) if committed_events else 0,
    )
