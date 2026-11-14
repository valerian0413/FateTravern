"""Creation review and optional semantic audit."""
from __future__ import annotations

import uuid
import json
from dataclasses import asdict, is_dataclass

from app.domain.changes import apply_to_copy
from app.domain.errors import ValidationError

from app.domain.types import Issue, IssueSeverity, Proposal, ProposalStatus
from app.prompts.loader import load
from app.providers.base import ModelRequest


def needs_semantic_audit(draft: dict) -> bool:
    changes = draft.get('changes') or []
    if any(c.get('op') in ('append_fact', 'supersede_fact', 'create_entity') for c in changes):
        # Trigger on secret-linked or multi-entity edits.
        if len(changes) >= 2 or any(c.get('disclosure_rule') for c in changes):
            return True
    return bool(draft.get('force_audit'))


def validate_result(draft: dict, snapshot) -> list[Issue]:
    issues: list[Issue] = []
    known = set(snapshot.entities)
    for index, change in enumerate(draft.get('changes') or []):
        if change.get('op') == 'create_entity' and change.get('id') in known:
            issues.append(Issue(IssueSeverity.BLOCKING, f'changes[{index}]', '实体已存在', '使用新ID'))
        for key, value in change.items():
            if key.endswith('_id') and isinstance(value, str) and value.startswith('new:'):
                issues.append(Issue(IssueSeverity.BLOCKING, f'changes[{index}].{key}', '未注册临时ID', '先 register'))
    return issues


async def review_creation(provider, settings, proposal_text: str, world_context: dict, base_version: int, world_id: str) -> Proposal:
    if provider is None or world_context.get('force_review'):
        data = world_context.get('force_review') or {
            'accepted': True,
            'proposed_text': proposal_text,
            'fact_candidates': [{'predicate': 'player_asserted', 'value': proposal_text}],
            'issues': [],
        }
    else:
        spec = load('cocreation_review', settings.prompt_version, settings.prompts_dir)
        response = await provider.complete(ModelRequest(
            module='cocreation_review',
            prompt_version=settings.prompt_version,
            schema_version='1',
            world_id=world_id,
            turn_id=world_context.get('turn_id'),
            call_id=uuid.uuid4().hex,
            messages=[
                {'role': 'system', 'content': spec.text + '\n只返回 JSON。'},
                {'role': 'user', 'content': str({'proposal': proposal_text, 'context': world_context})},
            ],
        ))
        data = response.content

    # Every proposal waits for explicit player confirmation.
    return Proposal(
        id=str(uuid.uuid4()),
        revision=1,
        world_id=world_id,
        base_version=base_version,
        original_text=proposal_text,
        proposed_text=data.get('proposed_text') or proposal_text,
        fact_candidates=list(data.get('fact_candidates') or []),
        issues=list(data.get('issues') or []),
        status=ProposalStatus.PENDING,
    )


async def semantic_audit(provider, settings, context: dict, draft: dict) -> list[Issue]:
    # Structural correctness belongs to deterministic code, not an LLM second-guessing UUIDs.
    snapshot = context['snapshot']
    apply_to_copy(snapshot, draft.get('changes') or [], source_event_id='audit-validation')
    if provider is None:
        return validate_result(draft, snapshot)
    spec = load('audit', settings.prompt_version, settings.prompts_dir)
    public = context.get('public')
    payload = {
        'structural_validation': 'passed',
        'setting': snapshot.world.setting,
        'before': {
            'entities': [asdict(e) for e in snapshot.entities.values()],
            'facts': [asdict(f) for f in snapshot.facts.values()],
            'npc_cards': [asdict(c) for c in snapshot.npc_cards.values()],
        },
        'changes': draft.get('changes') or [],
        'public': asdict(public) if is_dataclass(public) else public,
    }
    messages = [{'role': 'system', 'content': spec.text},
                {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]
    for attempt in range(2):
        response = await provider.complete(ModelRequest(
            module='audit', prompt_version=settings.prompt_version, schema_version='2',
            world_id=context.get('world_id'), turn_id=context.get('turn_id'), call_id=uuid.uuid4().hex,
            messages=messages,
        ))
        data = response.content
        try:
            if not isinstance(data, dict) or type(data.get('accepted')) is not bool or not isinstance(data.get('issues'), list):
                raise ValueError('缺少 accepted/issues')
            issues = []
            for item in data['issues']:
                if item.get('category') not in {'world_consistency', 'character_consistency', 'disclosure'}:
                    raise ValueError('只允许审核世界因果、人物行为和信息披露，不允许字段/ID/数据库结构检查')
                if not isinstance(item.get('reason'), str) or not item['reason'].strip():
                    raise ValueError('问题必须说明具体矛盾')
                issues.append(Issue(severity=IssueSeverity(item.get('severity', 'warning')),
                    path=item.get('path', ''), reason=item['reason'],
                    suggested_fix=item.get('suggested_fix', ''), reference_id=item.get('reference_id')))
            if data['accepted'] != (not any(i.severity == IssueSeverity.BLOCKING for i in issues)):
                raise ValueError('accepted 与 blocking 问题不一致')
            return issues
        except (ValueError, TypeError, AttributeError) as exc:
            if attempt:
                raise ValidationError('语义审核响应不符合职责约定：' + str(exc), path='audit') from None
            messages += [{'role': 'assistant', 'content': json.dumps(data, ensure_ascii=False)},
                         {'role': 'user', 'content': '请重新审核。' + str(exc)}]
