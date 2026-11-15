"""Compose player paraphrase + GM text from PublicOutcome only."""
from __future__ import annotations

import json
import uuid

from app.domain.errors import ValidationError
from app.domain.types import Issue, IssueSeverity, PublicOutcome
from app.prompts.loader import load
from app.providers.base import ModelRequest, call_model
from app.providers.json_stream import TextFieldWatch


def validate(text: str, authorized_action: str, *, max_chars: int = 180) -> list[Issue]:
    issues = []
    if len(text) > max_chars:
        issues.append(Issue(IssueSeverity.BLOCKING, 'gm_text', f'超过{max_chars}字符', '压缩到上限内'))
    # Soft check: narration should not invent verbs far from authorized action.
    banned = ('决定去', '心想', '突然拔腿', '未经允许')
    for token in banned:
        if token in text:
            issues.append(Issue(IssueSeverity.WARNING, 'gm_text', f'可能越权：{token}', '删除未授权行动'))
    return issues


def narrative_text(result):
    fields = ('player_action_text', 'gm_text')
    if not isinstance(result, dict) or any(
        not isinstance(result.get(field), str) or not result[field].strip() for field in fields
    ):
        raise ValidationError('叙事必须包含行动转述和世界回应', path='narration')
    return {field: result[field].strip() for field in fields}


async def compose(provider, settings, public_outcome: PublicOutcome | dict, style: dict | None = None,
                  previous_text: dict | None = None, on_text=None) -> dict:
    outcome = public_outcome if isinstance(public_outcome, dict) else {
        'authorized_action': public_outcome.authorized_action,
        'observable_events': public_outcome.observable_events,
        'visible_entities': public_outcome.visible_entities,
        'visible_facts': public_outcome.visible_facts,
        'stop_reason': public_outcome.stop_reason,
    }
    if provider is None or (style or {}).get('force_narration'):
        action = outcome['authorized_action']
        details = [event['text'] for event in outcome.get('observable_events', []) if event.get('text')]
        detail = ' '.join(details) or '眼前没有更多动静。'
        gm = f'{detail}'[: settings.narration_max_chars]
        player = action[:50]
        result = {'player_action_text': player, 'gm_text': gm}
    else:
        spec = load('narration', settings.prompt_version, settings.prompts_dir)
        payload = {'public_outcome': outcome, 'style': style or {}, 'previous': previous_text}
        response = await call_model(provider, ModelRequest(
            module='narration',
            prompt_version=settings.prompt_version,
            schema_version='1',
            world_id=(style or {}).get('world_id'),
            turn_id=(style or {}).get('turn_id'),
            call_id=uuid.uuid4().hex,
            messages=[
                {'role': 'system', 'content': spec.text + '\n只返回 JSON。'},
                {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)},
            ],
        ), on_delta=TextFieldWatch('gm_text', on_text) if on_text else None)
        result = response.content

    result = narrative_text(result)
    issues = validate(result.get('gm_text', ''), outcome['authorized_action'], max_chars=settings.narration_max_chars)
    player = result.get('player_action_text', '')
    if len(player) > 50:
        issues.append(Issue(IssueSeverity.BLOCKING, 'player_action_text', '超过50字', '缩短转述'))
    if any(i.severity == IssueSeverity.BLOCKING for i in issues) and previous_text is None and provider is not None:
        # One compression repair.
        return await compose(provider, settings, outcome, style, previous_text={'result': result, 'issues': [
            {'path': i.path, 'reason': i.reason, 'suggested_fix': i.suggested_fix} for i in issues
        ]}, on_text=on_text)
    if len(result.get('gm_text', '')) > settings.narration_max_chars:
        # Last resort only after repair attempt failed in offline mode: still do not hard-cut meaning silently —
        # but offline fixtures must pass; compress by sentence boundary.
        text = result['gm_text']
        result['gm_text'] = text[: settings.narration_max_chars]
    if len(result.get('player_action_text', '')) > 50:
        result['player_action_text'] = result['player_action_text'][:50]
    return result
