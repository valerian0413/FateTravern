"""Model decisions for a short, turn-based encounter."""
from __future__ import annotations

import json
import uuid

from app.domain.errors import ValidationError
from app.prompts.loader import load
from app.providers.base import ModelRequest
from app.rules.character import SKILLS


KINDS = {'combat': '战斗', 'chase': '追逐', 'negotiation': '谈判'}
ACTIONS = {'attack', 'advance', 'approach', 'reposition', 'guard', 'prepare', 'continue', 'withdraw', 'wait', 'clarify'}
DIFFICULTIES = {'easy', 'standard', 'hard', 'extreme'}


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


def action(raw, kind):
    if not isinstance(raw, dict) or not isinstance(raw.get('kind'), str) or raw['kind'] not in ACTIONS:
        raise ValidationError('遭遇动作类型无效', path='encounter.action')
    default_skill = {'combat': '战斗', 'chase': '运动', 'negotiation': '亲善'}[kind]
    attack = raw.get('skill')
    defense = raw.get('defense_skill')
    difficulty = raw.get('difficulty')
    rounds = raw.get('rounds')
    return {
        'kind': raw['kind'],
        'description': short(raw.get('description'), 80, '留意眼前局势'),
        'target_id': short(raw.get('target_id'), 100),
        'skill': attack if isinstance(attack, str) and attack in SKILLS else default_skill,
        'defense_skill': defense if isinstance(defense, str) and defense in SKILLS else (
            '意志' if kind == 'negotiation' else '运动'),
        'rounds': max(1, min(3, rounds)) if type(rounds) is int else 1,
        'difficulty': difficulty if isinstance(difficulty, str) and difficulty in DIFFICULTIES else 'standard',
        'injury': short(raw.get('injury'), 75, '在当前冲突中受伤'),
        'use_preparation': raw.get('use_preparation') is True,
        'equipment': short(raw.get('equipment'), 80),
    }


def skills(raw):
    if not isinstance(raw, dict):
        return {}
    return {name: value for name, value in raw.items()
            if name in SKILLS and type(value) is int and 0 <= value <= 3}
