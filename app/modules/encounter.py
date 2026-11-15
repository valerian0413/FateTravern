"""Model decisions for a short, turn-based encounter."""
from __future__ import annotations

import json
import uuid

from app.domain.errors import ValidationError
from app.prompts.loader import load
from app.providers.base import ModelRequest
from app.rules.character import SKILLS


KINDS = {'combat': '战斗', 'chase': '追逐', 'negotiation': '谈判'}
ACTIONS = {'attack', 'advance', 'approach', 'reposition', 'guard', 'observe', 'search', 'study',
           'prepare', 'continue', 'withdraw', 'wait', 'clarify'}
DIFFICULTIES = {'easy', 'standard', 'hard', 'extreme'}


def planned_actions(plan):
    """Stable local references for each unit's steps; persistent IDs stay unchanged."""
    for uid, first in plan.items():
        yield uid, uid, first
        for index, step in enumerate(first.get('followups', []), 1):
            yield f'{uid}:step:{index}', uid, step


def short(value, limit, default=''):
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else default


async def ask(runtime, turn, module, payload, fixture):
    if (provided := (runtime.fixtures or {}).get(module)) is not None:
        return provided
    if runtime.provider is None:
        return fixture
    spec = load(module, runtime.settings.prompt_version, runtime.settings.prompts_dir)
    response = await runtime.provider.complete(ModelRequest(
        module=module, prompt_version=spec.version, schema_version='1',
        world_id=turn.world_id, turn_id=turn.id, call_id=str(uuid.uuid4()),
        messages=[{'role': 'system', 'content': spec.text},
                  {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}],
        output_budget=2400,
    ))
    if not isinstance(response.content, dict):
        raise ValidationError('遭遇 Agent 必须返回 JSON 对象', path=module)
    return response.content


def action(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get('kind'), str) or raw['kind'] not in ACTIONS:
        raise ValidationError('遭遇动作类型无效', path='encounter.action')
    difficulty = raw.get('difficulty')
    rounds = raw.get('rounds')
    move = {
        'kind': raw['kind'],
        'description': short(raw.get('description'), 80, '留意眼前局势'),
        'target_id': short(raw.get('target_id'), 100),
        'weakness': short(raw.get('weakness'), 80),
        'rounds': max(1, min(3, rounds)) if type(rounds) is int else 1,
        'difficulty': difficulty if isinstance(difficulty, str) and difficulty in DIFFICULTIES else 'standard',
        'injury': short(raw.get('injury'), 75, '在当前冲突中受伤'),
        'use_preparation': raw.get('use_preparation') is True,
    }
    if move['kind'] == 'search':
        items = raw.get('search_items')
        if (not isinstance(items, list) or not 1 <= len(items) <= 3
                or any(not isinstance(item, str) or not item.strip() for item in items)):
            raise ValidationError('搜索须列出一至三件可能来得及辨认的已知物品', path='encounter.search_items')
        move['search_items'] = list(dict.fromkeys(item.strip() for item in items))
    return move


def skills(raw):
    if not isinstance(raw, dict):
        return {}
    return {name: value for name, value in raw.items()
            if name in SKILLS and type(value) is int and 0 <= value <= 3}
