"""Four-dice acceptance: 81 combos, duplicate modifiers, blocked, automatic."""
from __future__ import annotations

import itertools

import pytest

from app.domain.errors import ValidationError
from app.rules.four_dice import FourDice, roll


def test_roll_values():
    class Seq:
        def __init__(self):
            self.seq = [0, 1, 2, 1]
            self.i = 0

        def randrange(self, n):
            assert n == 3
            v = self.seq[self.i]
            self.i += 1
            return v

    assert roll(Seq()) == (-1, 0, 1, 0)


def test_81_combinations_easy_no_skill(four_dice: FourDice):
    actor = {'skills': {}}
    successes = 0
    for dice in itertools.product((-1, 0, 1), repeat=4):
        assessment = four_dice.assess(
            {'mode': 'check', 'difficulty': 'easy', 'domain': None, 'goal': '观察', 'conditions': []},
            actor,
        )
        result = four_dice.resolve(assessment, {'dice': list(dice), 'check_id': 'c1'})
        if result['success']:
            successes += 1
    assert successes == 50


def test_duplicate_modifier_rejected(four_dice: FourDice):
    with pytest.raises(ValidationError):
        four_dice.assess(
            {
                'mode': 'check', 'difficulty': 'standard', 'domain': '剑术', 'goal': '攻击',
                'conditions': [
                    {'source_id': 'cover', 'direction': 'advantage'},
                    {'source_id': 'cover', 'direction': 'disadvantage'},
                ],
            },
            {'skills': {'剑术': 2}},
        )


def test_blocked_no_dice(four_dice: FourDice):
    assessment = four_dice.assess(
        {'mode': 'blocked', 'goal': '穿过锁门', 'conditions': [], 'failure_effects': [{'op': 'noop'}]},
        {'skills': {}},
    )
    result = four_dice.resolve(assessment, {'check_id': 'c-blocked'})
    assert result['success'] is False
    assert result['dice'] == []


def test_automatic_success(four_dice: FourDice):
    assessment = four_dice.assess(
        {'mode': 'automatic', 'goal': '环顾', 'conditions': [], 'success_effects': []},
        {'skills': {'自然知识': 1}},
    )
    result = four_dice.resolve(assessment, {'check_id': 'c-auto'})
    assert result['success'] is True
    assert result['skill_bonus'] == 0


def test_negative_resource_not_via_rules(four_dice: FourDice):
    # Rules do not mutate resources; changes layer rejects negatives.
    assessment = four_dice.assess(
        {'mode': 'check', 'difficulty': 'hard', 'domain': '剑术', 'goal': '砍', 'conditions': []},
        {'skills': {'剑术': 2}},
    )
    result = four_dice.resolve(assessment, {'dice': [1, 1, 1, 1], 'check_id': 'c'})
    assert result['total'] == 6
    assert result['success'] is True
