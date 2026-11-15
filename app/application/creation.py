"""Prepare presets and publish the saved opening as a system event."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from app.api.notify import notify_world
from app.application.chat import Receipt
from app.application.opening import briefing
from app.domain.errors import NotFound
from app.domain.inventory import denominations, unpack_legacy_coins
from app.domain.types import Channel, Entity, EntityKind, Fact, FactStatus, Phase, Turn, TurnStage, World
from app.rules.four_dice import load_config
from app.modules.planning import validate_options
from app.storage.repositories import EntityRepo, EventRepo, FactRepo, MessageRepo, StreamRepo, TurnRepo, WorldRepo


def _load_preset(presets_dir, preset_id: str) -> dict:
    for path in sorted(presets_dir.glob('*.json')):
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('preset_id') == preset_id:
            return data
    raise NotFound(f'未知预设 {preset_id}')


async def prepare(runtime, preset_id: str, overrides: dict | None = None) -> World:
    preset = _load_preset(runtime.settings.presets_dir, preset_id)
    overrides = overrides or {}
    player_id = str(uuid.uuid4())
    world_id = str(uuid.uuid4())
    rules_config = load_config(runtime.settings.rules_path)
    world = World(
        id=world_id,
        name=overrides.get('name') or preset.get('name') or '未命名世界',
        setting=overrides.get('setting') or preset['setting'],
        phase=Phase.PREPARED,
        version=0,
        clock=0,
        ruleset_id=preset.get('ruleset_id') or rules_config.get('ruleset_id', 'four-dice-v1'),
        ruleset_config={**rules_config, 'opening_options': validate_options(preset.get('opening_options', [])),
                        'opening_guide': preset.get('opening_guide', {}),
                        'currency_denominations': denominations(preset.get('world_book'))},
        player_id=player_id,
    )

    temp_map: dict[str, str] = {}
    async with runtime.db.transaction():
        await WorldRepo(runtime.db.conn).create(world)
        player = preset['player']
        character = Entity(
            id=player_id, world_id=world_id, kind=EntityKind.PLAYER,
            name=player['name'],
            attributes={
                'identity': player.get('identity', ''),
                'skills': player.get('skills') or {},
                'resources': player.get('resources') or {},
                'opening_intent': preset.get('opening_intent', ''),
                'preset_id': preset.get('preset_id'),
            },
            revision=1,
        )
        entities = {player_id: character}
        unpack_legacy_coins(world, entities)
        for entity in entities.values():
            await EntityRepo(runtime.db.conn).upsert(entity)
        for place in preset.get('places') or []:
            eid = str(uuid.uuid4())
            temp_map[place['temp_id']] = eid
            await EntityRepo(runtime.db.conn).upsert(Entity(
                id=eid, world_id=world_id, kind=EntityKind.PLACE, name=place['name'],
                attributes=place.get('attributes') or {}, revision=1,
            ))
        for fact in preset.get('facts') or []:
            subject = temp_map.get(fact.get('subject_temp_id'), player_id)
            fid = str(uuid.uuid4())
            if fact.get('temp_id'):
                temp_map[fact['temp_id']] = fid
            disclosure = {'disclosure': 'public'} if fact.get('disclosure') == 'public' else fact.get('disclosure_rule')
            await FactRepo(runtime.db.conn).upsert(Fact(
                id=fid, world_id=world_id, subject_id=subject,
                predicate=fact.get('predicate', 'detail'), value=fact.get('value'),
                status=FactStatus.ACTIVE, disclosure_rule=disclosure,
            ))
    return world


async def start(runtime, world_id: str, request_id: str) -> Receipt:
    db = runtime.db.conn
    worlds, turns = WorldRepo(db), TurnRepo(db)
    async with runtime.chat.turns.lock_for(world_id):
        world = await worlds.get(world_id)
        existing = await runtime.db.fetchall(
            "SELECT t.request_id FROM events e JOIN turns t ON t.world_id=e.world_id AND t.id=e.turn_id "
            "WHERE e.world_id=? AND e.kind='opening' LIMIT 1", (world_id,),
        )
        if existing:
            return runtime.chat._receipt(world_id, existing[0][0])
        if world.phase != Phase.PREPARED or world.version != 0:
            return runtime.chat._receipt(world_id, request_id)

        snapshot = await worlds.get_snapshot(world_id)
        options = validate_options(world.ruleset_config.get('opening_options', []))
        # Old saves can already have a player turn with the same opening request ID.
        if await turns.get_by_request(world_id, request_id):
            request_id = str(uuid.uuid4())
        turn_id = str(uuid.uuid4())
        opening, options, prompt = await briefing(runtime, snapshot, options, turn_id)
        async with runtime.db.transaction():
            await turns.reserve(Turn(
                id=turn_id, world_id=world_id, request_id=request_id, payload_hash='system:opening',
                base_version=world.version, stage=TurnStage.COMPLETED, checkpoint={'source': 'system'},
                created_at=datetime.now(timezone.utc).isoformat(),
            ))
            messages = MessageRepo(db)
            scene_message = await messages.append(
                world_id, Channel.NARRATIVE, 'gm', opening, request_id=request_id, turn_id=turn_id,
            )
            choice_message = await messages.append(
                world_id, Channel.INTERACTION, 'gm', prompt, request_id=request_id, turn_id=turn_id,
            )
            result = {'kind': 'opening', 'messages': [scene_message, choice_message],
                      'options': options, 'prompt': prompt, 'base_version': world.version}
            await turns.checkpoint(world_id, turn_id, TurnStage.COMPLETED, {'source': 'system'}, result=result)
            await EventRepo(db).append(world_id, {
                'id': str(uuid.uuid4()), 'turn_id': turn_id, 'game_time': world.clock,
                'kind': 'opening', 'player_visible_summary': opening,
            })
            await worlds.set_phase(world_id, Phase.PLAYING)
            await StreamRepo(db).append(
                world_id, 'turn.committed',
                {'message_ids': [scene_message['id'], choice_message['id']], 'turn_id': turn_id},
                request_id=request_id,
            )
        notify_world(runtime, world_id)
        return runtime.chat._receipt(world_id, request_id)
