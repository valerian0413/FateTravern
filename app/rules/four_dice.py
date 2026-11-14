"""Four ternary dice ruleset: 4 × {-1,0,+1} + domain + environment vs DC."""
from __future__ import annotations

import json
import secrets
from pathlib import Path

from app.domain.changes import hash_inputs
from app.domain.errors import ValidationError


DEFAULT_TARGETS = {'easy': 0, 'standard': 2, 'hard': 4, 'extreme': 6}


def load_config(path: Path | None = None) -> dict:
    if path and path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    return {
        'ruleset_id': 'four-dice-v1',
        'targets': dict(DEFAULT_TARGETS),
        'environment_step': 1,
        'tie_succeeds': True,
    }


def roll(rng=None) -> tuple[int, int, int, int]:
    if rng is None:
        return tuple(secrets.randbelow(3) - 1 for _ in range(4))  # type: ignore[return-value]
    return tuple(rng.randrange(3) - 1 for _ in range(4))  # type: ignore[return-value]


class FourDice:
    version = 'four-dice-v1'

    def __init__(self, config: dict | None = None):
        self.config = config or load_config()
        self.version = self.config.get('ruleset_id', 'four-dice-v1')
        self.targets = dict(self.config.get('targets') or DEFAULT_TARGETS)
        self.environment_step = int(self.config.get('environment_step', 1))
        self.tie_succeeds = bool(self.config.get('tie_succeeds', True))

    def assess(self, challenge: dict, actor: dict) -> dict:
        mode = challenge.get('mode')
        if mode not in ('automatic', 'check', 'blocked', 'opposed'):
            raise ValidationError('非法判定方式', path='challenge.mode')
        conditions = challenge.get('conditions') or challenge.get('modifiers') or []
        sources = set()
        modifiers = []
        for condition in conditions:
            source = condition['source_id']
            direction = condition.get('direction')
            if source in sources or direction not in ('advantage', 'disadvantage'):
                raise ValidationError('重复或非法环境条件', path='challenge.conditions')
            sources.add(source)
            if self.version == 'fate-lite-v1' and source.startswith('aspect:'):
                aspects = {a['id']:a for a in actor.get('aspects', [])}
                if source not in aspects or not condition.get('reason') or not condition.get('basis_id'):
                    raise ValidationError('形象修正需要合法形象、原因和同因标识', path='challenge.conditions')
            basis = condition.get('basis_id', source)
            if any(m.get('basis_id') == basis for m in modifiers):
                raise ValidationError('同一原因不能重复计入修正', path='challenge.conditions')
            step = self.environment_step * (1 if direction == 'advantage' else -1)
            modifiers.append({'source_id': source, 'direction': direction, 'value': step,
                              'basis_id': basis, 'reason': condition.get('reason', '') if source.startswith('aspect:') or condition.get('public') is True else '',
                              'label': next((a['text'] for a in actor.get('aspects',[]) if a['id']==source), '')})
        domain = challenge.get('domain')
        bonus = actor.get('skills', {}).get(domain, 0) if domain else 0
        if type(bonus) is not int:
            raise ValidationError('领域加值必须是整数', path='actor.skills')
        if mode not in ('check', 'opposed'):
            bonus = 0
            modifiers = []
            domain = None
            difficulty = None
        elif mode == 'check':
            difficulty = challenge.get('difficulty')
            if difficulty not in self.targets:
                raise ValidationError('非法难度', path='challenge.difficulty')
        else:
            difficulty = None
        return {
            'mode': mode,
            'goal': challenge.get('goal', ''),
            'domain': domain,
            'difficulty': difficulty,
            'skill_bonus': bonus,
            'modifiers': modifiers,
            'prerequisites': list(challenge.get('prerequisites') or []),
            'success_effects': list(challenge.get('success_effects') or []),
            'failure_effects': list(challenge.get('failure_effects') or []),
            'duration_minutes': int(challenge.get('duration_minutes') or 0),
            'challenge': challenge,
            'actor': {'skills': actor.get('skills', {}), **({'aspects':actor.get('aspects',[])} if self.version=='fate-lite-v1' else {})},
        }

    def resolve(self, assessment: dict, random_record: dict | None = None) -> dict:
        mode = assessment['mode']
        dice = None
        if random_record and 'dice' in random_record:
            dice = list(random_record['dice'])
        defense_dice = None
        damage = None
        if mode in ('check', 'opposed'):
            if dice is None:
                dice = list(roll())
            if len(dice) != 4 or any(d not in (-1, 0, 1) for d in dice):
                raise ValidationError('需要四个取值为-1、0、1的骰子', path='dice')
            if mode == 'opposed':
                defense_dice = (random_record or {}).get('payload', {}).get('defense_dice')
                if defense_dice is None:
                    defense_dice = list(roll())
                if len(defense_dice) != 4 or any(d not in (-1, 0, 1) for d in defense_dice):
                    raise ValidationError('防守需要四颗有效骰子', path='defense_dice')
                dc = sum(defense_dice) + assessment['defense_bonus']
            else:
                dc = self.targets[assessment['difficulty']]
            total = sum(dice) + assessment['skill_bonus'] + sum(m['value'] for m in assessment['modifiers'])
            success = total >= dc if self.tie_succeeds else total > dc
            margin = total - dc
            if mode == 'opposed':
                damage = max(0, margin)
                success = damage > 0
        else:
            dice = []
            dc = None
            total = None
            margin = None
            success = mode == 'automatic'
        input_hash = hash_inputs({
            'challenge': assessment.get('challenge'),
            'actor': assessment.get('actor'),
            'config': {
                'targets': self.targets,
                'environment_step': self.environment_step,
                'tie_succeeds': self.tie_succeeds,
            },
        })
        effects = assessment['success_effects'] if success else assessment['failure_effects']
        return {
            'check_id': (random_record or {}).get('check_id') or assessment.get('check_id'),
            'input_hash': input_hash,
            'ruleset_version': self.version,
            'mode': mode,
            'dice': dice,
            **({'defense_dice': defense_dice, 'damage': damage, 'attack_total': total,
                'defense_total': dc} if mode == 'opposed' else {}),
            'skill_bonus': assessment['skill_bonus'],
            'modifiers': assessment['modifiers'],
            'total': total,
            'dc': dc,
            'margin': margin,
            'success': success,
            'selected_effects': effects,
            'domain': assessment.get('domain'),
            'difficulty': assessment.get('difficulty'),
            'duration_minutes': assessment.get('duration_minutes', 0),
        }
