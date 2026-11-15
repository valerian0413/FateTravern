"""Opposed hits and legal ways to absorb their shifts."""
import re
from itertools import combinations

from app.domain.stress import CAPACITIES, stress_boxes


def incapacitating_consequence(text):
    """Recognize explicit declarations that a participant can no longer act."""
    return bool(re.fullmatch(
        r'(?:我|角色)?\s*(?:已(?:经)?|陷入|当场|直接)?\s*'
        r'(?:昏迷(?:不醒)?|失去意识|无法(?:行动|继续战斗)|死亡|出局)[。！!\s]*',
        text.strip(),
    ))


def absorptions(attributes, damage):
    boxes = stress_boxes(attributes)
    if boxes is None:
        boxes = [False] * 3
    scars = attributes.get('consequences', {})
    available = [slot for slot in CAPACITIES if not scars.get(slot, {}).get('text')]
    choices = []
    for box in [None, *(index for index, used in enumerate(boxes) if not used)]:
        for count in range(len(available) + 1):
            for slots in combinations(available, count):
                capacity = (box + 1 if box is not None else 0) + sum(CAPACITIES[slot] for slot in slots)
                if capacity >= damage:
                    choices.append((box, slots, capacity))
    return sorted(choices, key=lambda choice: (
        'severe' in choice[1], 'moderate' in choice[1], len(choice[1]), choice[2],
    ))


def damage_effects(entity_id, selection, injury):
    if selection is None:
        return [{'op': 'taken_out', 'op_id': 'combat-taken-out', 'entity_id': entity_id}]
    box, slots, _ = selection
    effects = []
    if box is not None:
        effects.append({'op': 'mark_stress', 'op_id': 'combat-pressure', 'entity_id': entity_id,
                        'boxes': [box]})
    effects.extend({'op': 'take_consequence', 'op_id': f'combat-scar-{slot}',
                    'entity_id': entity_id, 'slot': slot, 'text': injury}
                   for slot in slots)
    return effects
