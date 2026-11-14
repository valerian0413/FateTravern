"""HTTP route handlers — parse and call services only."""
from __future__ import annotations

from app.api.schemas import decode_request, encode_response
from app.application import archives, creation, recovery
from app.application.encounters import active_encounter, public_encounter
from app.domain.errors import Conflict, NotFound, StaleProposal
from app.domain.stress import stress_boxes
from app.domain.types import ProposalStatus
from app.modules.visibility import filter_for
from app.storage.repositories import EncounterRepo, MessageRepo, ProposalRepo, TurnRepo, WorldRepo


async def list_worlds(runtime, _request):
    rows = await WorldRepo(runtime.db.conn).list()
    return 200, rows


async def delete_world(runtime, request):
    await archives.delete_archive(runtime, request.match_info['world_id'])
    return 200, {'deleted': True}


async def list_demos(runtime, _request):
    from app.application import demos
    return 200, demos.list_demos(runtime)


async def play_demo(runtime, request):
    from app.application import demos
    world = await demos.play(runtime, request.match_info['demo_id'])
    return 201, {
        'id': world.id,
        'name': world.name,
        'phase': world.phase.value,
        'version': world.version,
        'player_id': world.player_id,
    }


async def create_world(runtime, request):
    body = decode_request('create_world', await request.json())
    world = await creation.prepare(runtime, body['preset_id'], body.get('overrides'))
    return 201, {
        'id': world.id,
        'name': world.name,
        'phase': world.phase.value,
        'version': world.version,
        'player_id': world.player_id,
    }


async def start_world(runtime, request):
    world_id = request.match_info['world_id']
    body = decode_request('start_world', await request.json())
    receipt = await creation.start(runtime, world_id, body['request_id'])
    return 202, encode_response(receipt)


async def send_message(runtime, request):
    world_id = request.match_info['world_id']
    body = decode_request('send_message', await request.json())
    receipt = await runtime.chat.submit(
        world_id, body['request_id'], body['text'], body.get('mode_hint'), body.get('reply_to'), combat_description=body.get('combat_description'),
    )
    return 202, encode_response(receipt)


async def confirm_proposal(runtime, request):
    world_id = request.match_info['world_id']
    pid = request.match_info['proposal_id']
    body = decode_request('confirm_proposal', await request.json())
    if active_encounter(await EncounterRepo(runtime.db.conn).get(world_id)):
        raise Conflict('请先结束当前遭遇，再确认设定提案')
    proposal = await ProposalRepo(runtime.db.conn).get(world_id, pid)
    if not proposal:
        raise NotFound('提案不存在')
    snapshot = await WorldRepo(runtime.db.conn).get_snapshot(world_id)
    if proposal.revision != body['revision'] or proposal.base_version != body['base_version']:
        raise StaleProposal('提案版本过期，请重新审核')
    if proposal.base_version != snapshot.world.version:
        raise StaleProposal('世界已变化，提案过期')
    proposal.status = ProposalStatus.ACCEPTED
    await ProposalRepo(runtime.db.conn).update(proposal)
    await runtime.db.conn.commit()
    # Accepted setting enters this turn as explore-like action with creation facts.
    text = proposal.proposed_text
    receipt = await runtime.chat.submit(
        world_id, body['request_id'], text, mode_hint='行动', confirmed_action=True,
    )
    return 202, encode_response(receipt)


async def reject_proposal(runtime, request):
    world_id = request.match_info['world_id']
    pid = request.match_info['proposal_id']
    body = decode_request('reject_proposal', await request.json())
    proposal = await ProposalRepo(runtime.db.conn).get(world_id, pid)
    if not proposal:
        raise NotFound('提案不存在')
    if proposal.revision != body['revision']:
        raise StaleProposal('提案版本不匹配')
    proposal.status = ProposalStatus.REJECTED
    await ProposalRepo(runtime.db.conn).update(proposal)
    await runtime.db.conn.commit()
    return 200, {
        'id': proposal.id, 'revision': proposal.revision, 'status': proposal.status.value,
        'proposed_text': proposal.proposed_text,
    }


async def get_request(runtime, request):
    world_id = request.match_info['world_id']
    rid = request.match_info['request_id']
    turn = await TurnRepo(runtime.db.conn).get_by_request(world_id, rid)
    if not turn:
        raise NotFound('请求不存在')
    return 200, {
        'request_id': turn.request_id,
        'status': turn.stage.value,
        'stage': turn.stage.value,
        'result': turn.result,
        'error': turn.error_code,
        'retryable': turn.stage.value == 'failed_retryable',
    }


async def retry_request(runtime, request):
    world_id = request.match_info['world_id']
    rid = request.match_info['request_id']
    receipt = await recovery.retry(runtime, world_id, rid)
    return 202, encode_response(receipt)


async def cancel_request(runtime, request):
    world_id = request.match_info['world_id']
    rid = request.match_info['request_id']
    return 200, await recovery.cancel(runtime, world_id, rid)


async def get_messages(runtime, request):
    world_id = request.match_info['world_id']
    channel = request.rel_url.query.get('channel')
    after = int(request.rel_url.query.get('after') or 0)
    rows = await MessageRepo(runtime.db.conn).list(world_id, channel=channel, after=after)
    return 200, rows


async def get_view(runtime, request):
    world_id = request.match_info['world_id']
    snapshot = await WorldRepo(runtime.db.conn).get_snapshot(world_id)
    player = snapshot.entities[snapshot.world.player_id]
    visible_facts = filter_for(snapshot.world.player_id, snapshot.facts, snapshot.knowledge)
    return 200, {
        'world_id': world_id,
        'name': snapshot.world.name,
        'phase': snapshot.world.phase.value,
        'version': snapshot.world.version,
        'clock': snapshot.world.clock,
        'setting': snapshot.world.setting,
        'encounter': public_encounter(active_encounter(await EncounterRepo(runtime.db.conn).get(world_id))),
        'character': {
            'id': player.id,
            'name': player.name,
            'ruleset_id': snapshot.world.ruleset_id,
            'aspects': player.attributes.get('aspects', []),
            'stress_tracks': {'unified': boxes} if (boxes := stress_boxes(player.attributes)) is not None else None,
            'consequences': player.attributes.get('consequences'),
            'fate_points': player.attributes.get('fate_points', 0),
            'skills': player.attributes.get('skills', {}),
            'resources': {'hp':3,'stress':3,**player.attributes.get('resources', {})},
            'inventory': [e.name for e in snapshot.entities.values() if e.kind.value=='item' and e.attributes.get('owner')==player.id],
        },
        'visible_facts': [
            {'id': f.id, 'predicate': f.predicate, 'value': f.value, 'subject_id': f.subject_id}
            for f in visible_facts
        ],
        'entities': [
            {'id': e.id, 'kind': e.kind.value, 'name': e.name}
            for e in snapshot.entities.values()
            if e.kind.value != 'player'
        ],
    }


async def workshop_list(runtime, request):
    cursor = await runtime.db.conn.execute('SELECT id,revision,stage,payload FROM workshop ORDER BY rowid DESC')
    import json
    rows = []
    for row in await cursor.fetchall():
        item = dict(row)
        data = json.loads(item.pop('payload'))
        item['name'] = data['book']['name']
        rows.append(item)
    return 200, rows


async def workshop_create(runtime, request):
    from app.application import workshop
    body = await request.json()
    if not isinstance(body, dict):
        from app.domain.errors import ValidationError
        raise ValidationError('请求必须是对象')
    return 201, await workshop.create(runtime, body.get('source'))


async def workshop_get(runtime, request):
    from app.application import workshop
    return 200, await workshop.get(runtime, request.match_info['draft_id'])


async def workshop_message(runtime, request):
    from app.application import workshop
    return 200, await workshop.message(runtime, request.match_info['draft_id'], await request.json())


async def workshop_confirm(runtime, request):
    from app.application import workshop
    return 200, await workshop.confirm(runtime, request.match_info['draft_id'], await request.json())


async def workshop_update_document(runtime, request):
    from app.application import workshop
    return 200, await workshop.update_document(runtime, request.match_info['draft_id'], await request.json())


async def workshop_catalog(runtime, request):
    from app.application import workshop
    return 200, await workshop.catalog(runtime)


async def workshop_delete_setting(runtime, request):
    from app.application import workshop
    await workshop.delete_setting(runtime, request.match_info['kind'], request.match_info['setting_id'])
    return 200, {'deleted': True}
