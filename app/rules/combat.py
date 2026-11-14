"""Opposed hits and legal ways to absorb their shifts."""
from itertools import combinations

from app.domain.stress import CAPACITIES, stress_boxes


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
