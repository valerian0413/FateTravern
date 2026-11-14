"""Intent recognition — one model call, no story writing."""
from __future__ import annotations

import uuid
import json

from app.domain.types import Intent, IntentKind
from app.prompts.loader import load
from app.providers.base import ModelRequest


async def recognize(provider, settings, context: dict, text: str, mode_hint: str | None) -> Intent:
    # Deterministic shortcuts for tests / offline fixtures.
    if context.get('force_intent'):
        data = context['force_intent']
        return _to_intent(data)

    hint = (mode_hint or 'auto').strip()
    if context.get('provider') is None and provider is None:
        return _heuristic(text, hint)

    spec = load('intent', settings.prompt_version, settings.prompts_dir)
    request = ModelRequest(
        module='intent',
        prompt_version=settings.prompt_version,
        schema_version='1',
        world_id=context.get('world_id'),
        turn_id=context.get('turn_id'),
        call_id=uuid.uuid4().hex,
        messages=[
            {'role': 'system', 'content': spec.text + '\n只返回一个 JSON 对象。'},
            {'role': 'user', 'content': json.dumps({'text':text,'mode_hint':hint,'public':context.get('public',{}),'history':context.get('history',[])},ensure_ascii=False)},
        ],
    )
    response = await provider.complete(request)
    return _to_intent(response.content)


def _heuristic(text: str, hint: str) -> Intent:
    lowered = text
    if hint == '共创' or any(k in lowered for k in ('我想这里有', '设定', '应该有一位')):
        return Intent(
            kind=IntentKind.COCREATE, target_ids=[], creation_statements=[text],
            needs_clarification=False, requested_tools=['review'], normalized_action=None,
        )
    if hint == '交流' or any(k in lowered for k in ('吗', '能不能', '可以', '会不会', '怎么')):
        if hint != '行动':
            return Intent(
                kind=IntentKind.DISCUSSION, target_ids=[], creation_statements=[],
                needs_clarification=False, requested_tools=[], normalized_action=None, question=text,
            )
    if any(k in lowered for k in ('看看', '观察', '走向', '打开', '尝试', '我')):
        return Intent(
            kind=IntentKind.EXPLORE, target_ids=[], creation_statements=[],
            needs_clarification=False, requested_tools=['generation', 'visibility'],
            normalized_action=text.strip(),
        )
    return Intent(
        kind=IntentKind.CLARIFY, target_ids=[], creation_statements=[],
        needs_clarification=True, requested_tools=[], question='你是想讨论规则、执行行动，还是提出新设定？',
    )


def _to_intent(data: dict) -> Intent:
    kind = IntentKind(data.get('kind', 'clarify'))
    gaps = data.get('unresolved') or []
    typed = any(isinstance(g,dict) for g in gaps)
    world_gaps = [g['detail'] for g in gaps if isinstance(g,dict) and g.get('kind')=='world_fact' and isinstance(g.get('detail'),str)] if typed else list(gaps)
    tools = list(data.get('requested_tools') or [])
    if typed and not world_gaps: tools = [t for t in tools if t!='generation']
    return Intent(
        kind=kind,
        normalized_action=data.get('normalized_action'),
        target_ids=list(data.get('target_ids') or []),
        creation_statements=list(data.get('creation_statements') or []),
        needs_clarification=bool(data.get('needs_clarification')),
        question=data.get('question'),
        requested_tools=tools,
        unresolved=world_gaps,
    )
