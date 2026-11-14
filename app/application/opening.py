"""Build a first-session briefing from the saved public world and character."""
from app.application.context import public_book
from app.domain.types import EntityKind


def briefing(snapshot, options):
    world = snapshot.world
    rules = world.ruleset_config
    player = snapshot.entities[world.player_id]
    card = player.attributes
    scene = (rules.get('world_book') or {}).get('initial_scene') or {}
    place = snapshot.entities.get(card.get('location_id'))
    if place is None and card.get('preset_id'):
        place = next((entity for entity in snapshot.entities.values() if entity.kind == EntityKind.PLACE), None)
    location = scene.get('name') or (place.name if place else world.name)
    sections = (public_book(snapshot) or {}).get('sections', {})
    guide = rules.get('opening_guide') or {}

    def lore(*keys, limit=2):
        return ' '.join(dict.fromkeys(entry['text'] for key in keys for entry in sections.get(key, [])[:limit]))

    scene_text = scene.get('description') or (place.attributes.get('description') if place else '')
    opening = (scene.get('opening') or '').strip()
    if opening and not opening.startswith('我'):
        scene_text = opening
    identity = next((aspect['text'] for aspect in card.get('aspects', [])
                     if aspect.get('kind') == 'high_concept'), None)
    identity = identity or card.get('identity') or player.name
    motivation = card.get('motivation')
    character = f'你是{identity.rstrip("。！？")}。'
    if card.get('background'):
        character += f' {card["background"]}'
    if motivation:
        character += f' 此行的目标是{motivation.rstrip("。！？")}。'
    else:
        character += f' 眼下你到了{location}，可以先弄清周围的情况，再决定接下来做什么。'

    directions = options or [
        '我先观察当前地点，看看有哪些值得留意的人或事',
        '我向附近的人打听这里的规矩和最近有什么活计',
    ]
    groups = [
        (f'世界背景 · {world.name}', guide.get('background') or lore('background') or world.setting),
        (f'故事从这里开始 · {location}', guide.get('scene') or scene_text or f'你抵达了{location}。'),
        ('这里有哪些人', guide.get('people') or lore('factions', 'species', limit=1)
         or '眼下还没有熟人。你可以先观察周围有哪些人，再找合适的人搭话。'),
        ('这里有什么特色', guide.get('features') or lore('geography', 'supernatural', limit=1)
         or lore('tone', 'transport', limit=1) or '先留意这里的地形、交通和人们交换物资的方式。'),
        ('可以从哪些委托入手', guide.get('jobs')
         or '可以打听有没有人需要探路、送货、采集或查证传闻。是否有这样的委托，以及条件和报酬，要向当地人确认。'),
        ('你是谁，为什么来到这里', character),
    ]
    prompt = '带着这个目标，你想先做什么？' if motivation else '眼下你想先从哪里着手？'
    return '\n\n'.join(f'{heading}\n{text}' for heading, text in groups if text), directions, prompt
