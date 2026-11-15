"""Compose the first scene from public world settings and the saved character."""
import json
import uuid

from app.application.context import public_book
from app.domain.errors import ModelUnavailable, ValidationError
from app.domain.types import EntityKind
from app.modules.planning import validate_options
from app.prompts.loader import load
from app.providers.base import ModelRequest, call_model


def _opening_context(snapshot):
    world = snapshot.world
    rules = world.ruleset_config
    player = snapshot.entities[world.player_id]
    card = player.attributes
    scene = (rules.get('world_book') or {}).get('initial_scene') or {}
    place = snapshot.entities.get(card.get('location_id'))
    if place is None and card.get('preset_id'):
        place = next((entity for entity in snapshot.entities.values() if entity.kind == EntityKind.PLACE), None)
    book = public_book(snapshot)
    sections = book['sections'] if book else {}
    guide = rules.get('opening_guide') or {}
    return {
        'world': {
            'name': world.name,
            'background': [entry['text'] for entry in sections.get('background', [])] if book else [world.setting],
            'tone': [entry['text'] for entry in sections.get('tone', [])],
            'guide': guide.get('background'),
        },
        'character': {
            'name': player.name,
            'identity': next((aspect['text'] for aspect in card.get('aspects', [])
                              if aspect.get('kind') == 'high_concept'), None) or card.get('identity'),
            'background': card.get('background'),
            'motivation': card.get('motivation'),
            'equipment': card.get('equipment', []),
        },
        'scene': {
            'location': scene.get('name') or (place.name if place else None),
            'description': scene.get('description') or (place.attributes.get('description') if place else None),
            'opening': scene.get('opening') or card.get('opening_intent'),
            'guide': guide.get('scene'),
        },
    }


async def briefing(runtime, snapshot, options, turn_id):
    if runtime.provider is None:
        raise ModelUnavailable('模型不可用，无法生成开场。')

    settings = runtime.settings
    spec = load('opening', settings.prompt_version, settings.prompts_dir)
    payload = {**_opening_context(snapshot), 'opening_options': options}
    response = await call_model(runtime.provider, ModelRequest(
        module='opening', prompt_version=spec.version, schema_version='1',
        world_id=snapshot.world.id, turn_id=turn_id, call_id=uuid.uuid4().hex,
        output_budget=900, thinking='disabled',
        messages=[
            {'role': 'system', 'content': spec.text},
            {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)},
        ],
    ))
    result = response.content
    fields = ('background', 'character', 'scene', 'question')
    if not isinstance(result, dict) or any(
        not isinstance(result.get(field), str) or not result[field].strip() for field in fields
    ):
        raise ModelUnavailable('模型未能生成完整的开场，请重试。')
    if not options:
        try:
            options = validate_options(result.get('options'), required=True)
        except ValidationError as exc:
            raise ModelUnavailable('模型未能生成有效的开场选项，请重试。') from exc

    opening = '\n\n'.join(' '.join(result[field].splitlines()).strip() for field in fields[:-1])
    return opening, options, result['question'].strip()
