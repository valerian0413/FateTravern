"""Single write path for formal world state."""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.domain.changes import apply_to_copy, validate_changes, stabilize_changes
from app.domain.errors import Conflict, ValidationError
from app.domain.types import (
    Channel,
    DisclosureGrant,
    EntityKind,
    Issue,
    IssueSeverity,
    KnowledgeBasis,
    Phase,
    Snapshot,
    TurnDraft,
    TurnStage,
)
from app.modules.npc import build_card
from app.api.notify import notify_world
from app.storage.repositories import (
    CheckRepo,
    EntityRepo,
    EventRepo,
    FactRepo,
    JobRepo,
    KnowledgeRepo,
    MessageRepo,
    NpcCardRepo,
    RelationRepo,
    ScheduledRepo,
    StreamRepo,
    TurnRepo,
    WorldRepo,
)


@dataclass
class CommittedTurn:
    turn_id: str
    request_id: str
    version: int
    messages: list[dict]
    public_outcome: dict
    stop_reason: str


def validate(draft: TurnDraft | dict, snapshot: Snapshot) -> list[Issue]:
    data = draft if isinstance(draft, dict) else {
        'base_version': draft.base_version,
        'changes': draft.changes,
        'narration': draft.narration,
        'public_outcome': draft.public_outcome,
    }
    issues: list[Issue] = []
    if data.get('base_version') != snapshot.world.version:
        issues.append(Issue(IssueSeverity.BLOCKING, 'base_version', '世界版本过期', '重新规划'))
    issues.extend(validate_changes(data.get('changes') or [], snapshot))
    narration = data.get('narration') or {}
    if not narration.get('player_action_text') or not narration.get('gm_text'):
        issues.append(Issue(IssueSeverity.BLOCKING, 'narration', '缺少双通道叙事', '补全 player/gm 文本'))
    return issues


async def commit_turn(
    db,
    *,
    world_id: str,
    turn_id: str,
    request_id: str,
    draft: TurnDraft,
    grants: list[DisclosureGrant] | None = None,
    elapsed_minutes: int = 0,
    runtime=None,
) -> CommittedTurn:
    world_repo = WorldRepo(db)
    snapshot = await world_repo.get_snapshot(world_id)
    draft.changes = stabilize_changes(draft.changes, world_id, turn_id, existing_fact_ids=snapshot.facts)
    issues = validate(draft, snapshot)
    blocking = [i for i in issues if i.severity == IssueSeverity.BLOCKING]
    if blocking:
        raise ValidationError(blocking[0].reason, path=blocking[0].path)

    event_id = str(uuid.uuid4())
    next_state = apply_to_copy(snapshot, draft.changes, source_event_id=event_id)
    new_clock = snapshot.world.clock + max(0, elapsed_minutes)
    grant_list = grants or []

    for grant in grant_list:
        already = any(
            c.get('op') == 'learn' and c.get('fact_id') == grant.fact_id and c.get('observer_id') == grant.observer_id
            for c in draft.changes
        )
        if not already:
            next_state = apply_to_copy(next_state, [{
                'op': 'learn', 'op_id': f'grant-{grant.fact_id}', 'observer_id': grant.observer_id,
                'fact_id': grant.fact_id, 'basis': KnowledgeBasis.OBSERVED.value,
                'source_event_id': event_id,
            }], source_event_id=event_id)

    try:
        for entity in next_state.entities.values():
            await EntityRepo(db).upsert(entity)
            cards = draft.registered_candidates.get('npc_cards', {})
            if entity.kind == EntityKind.NPC and entity.id in cards and entity.id not in next_state.npc_cards:
                await NpcCardRepo(db).upsert(world_id, build_card({'id': entity.id, **cards[entity.id]}))
            elif entity.kind == EntityKind.NPC and entity.id in next_state.npc_cards:
                await NpcCardRepo(db).upsert(world_id, next_state.npc_cards[entity.id])

        for fact in next_state.facts.values():
            await FactRepo(db).upsert(fact)
        for kid, knowledge in next_state.knowledge.items():
            if kid not in snapshot.knowledge:
                await KnowledgeRepo(db).insert(knowledge)
        for rid, rel in next_state.relations.items():
            if rid not in snapshot.relations:
                await RelationRepo(db).insert(rel, world_id)
        for scheduled in next_state.scheduled.values():
            await ScheduledRepo(db).upsert(scheduled)

        await EventRepo(db).append(world_id, {
            'id': event_id,
            'turn_id': turn_id,
            'game_time': new_clock,
            'kind': 'encounter_turn' if draft.encounter else 'player_turn',
            'actor_ids': [snapshot.world.player_id],
            'changes': draft.changes,
            'evidence_ids': [g.fact_id for g in grant_list],
            'player_visible_summary': draft.narration.get('gm_text', ''),
        })

        player_msg = await MessageRepo(db).append(
            world_id, Channel.NARRATIVE, 'player', draft.narration['player_action_text'],
            request_id=request_id, turn_id=turn_id,
        )
        gm_msg = await MessageRepo(db).append(
            world_id, Channel.NARRATIVE, 'gm', draft.narration['gm_text'],
            request_id=request_id, turn_id=turn_id,
        )
        messages = [player_msg, gm_msg]
        for roll in draft.encounter_checks:
            await CheckRepo(db).save(world_id, {
                'check_id': roll['check_id'], 'turn_id': turn_id, 'input_hash': roll['input_hash'],
                'ruleset_version': roll['ruleset_version'], 'dice': roll['dice'], 'payload': roll,
            })
            if roll['mode'] == 'opposed':
                attack = f"{roll['actor']} {roll['domain']} {roll['skill_bonus']:+d}"
                defense = roll['target']
                if roll.get('defense_domain'):
                    defense += f" 防守（{roll['defense_domain']}，合计 {roll['defense_bonus']:+d}）"
                modifiers = sum(modifier['value'] for modifier in roll['modifiers'])
                if modifiers:
                    attack += f"，情境 {modifiers:+d}"
                explanation = (f"{attack}，掷出 {roll['dice']}（总值 {roll['attack_total']}）\n"
                               f"{defense}，掷出 {roll['defense_dice']}（总值 {roll['defense_total']}）\n"
                               f"差值 {roll['margin']:+d}。")
            else:
                explanation = (f"{roll['actor']} {roll['domain']} {roll['skill_bonus']:+d}，"
                               f"掷出 {roll['dice']}（总值 {roll['total']}），"
                               f"难度 {roll['dc']}，{'成功' if roll['success'] else '失败'}。")
            if roll.get('skill_reason'):
                explanation += '\n依据：' + roll['skill_reason']
            messages.append(await MessageRepo(db).append(
                world_id, Channel.INTERACTION, 'gm', explanation, request_id=request_id, turn_id=turn_id,
            ))
        check = draft.check_result
        if check and check['mode'] == 'check':
            modifiers = check['modifiers']
            explanation = '；'.join(f"{m.get('label') or '情境修正'}：{m.get('reason') or '修正'} {m['value']:+d}" for m in modifiers)
            text = (f"骰子 {check['dice']}，合计 {sum(check['dice']):+d}；技能 {check['skill_bonus']:+d}；"
                    f"情境 {sum(m['value'] for m in modifiers):+d}。\n"
                    f"总值 {check['total']} {'≥' if check['success'] else '<'} DC {check['dc']}，"
                    f"{'成功' if check['success'] else '失败'}。" + ('\n'+explanation if explanation else ''))
            messages.append(await MessageRepo(db).append(world_id, Channel.INTERACTION, 'gm', text,
                                                         request_id=request_id, turn_id=turn_id))
        if check and check['mode'] == 'opposed':
            text = (f"攻击骰 {check['dice']}，总值 {check['attack_total']}。"
                    f"防守骰 {check['defense_dice']}，总值 {check['defense_total']}。"
                    f"{check['damage']} 点伤害。")
            messages.append(await MessageRepo(db).append(
                world_id, Channel.INTERACTION, 'gm', text, request_id=request_id, turn_id=turn_id,
            ))
        options = draft.options
        from app.modules.planning import validate_options
        if not draft.combat_decision or len(options) != 1:
            validate_options(options, required=bool(draft.choice_prompt))
        if options:
            prompt = draft.choice_prompt or '接下来你想怎么做？你也可以自由描述其他行动。'
            choice_msg = await MessageRepo(db).append(world_id, Channel.INTERACTION, 'gm', prompt,
                                                     request_id=request_id, turn_id=turn_id)
            messages.append(choice_msg)

        outcome = draft.public_outcome
        result = {
            'messages': messages,
            'public_outcome': {
                'authorized_action': outcome.authorized_action,
                'observable_events': outcome.observable_events,
                'visible_entities': outcome.visible_entities,
                'visible_facts': outcome.visible_facts,
                'stop_reason': 'new_choice' if options else outcome.stop_reason,
            },
            'stop_reason': 'new_choice' if options else outcome.stop_reason,
            'options': options,
            'prompt': draft.choice_prompt,
            'base_version': snapshot.world.version + 1,
        }
        if draft.encounter:
            result['encounter'] = draft.encounter
        if draft.combat_decision:
            result['combat_decision'] = draft.combat_decision
        await TurnRepo(db).checkpoint(world_id, turn_id, TurnStage.COMPLETED, {'committed': True}, result=result)

        for job in draft.next_jobs:
            await JobRepo(db).enqueue(world_id, job['type'], job.get('payload') or {}, job['dedup_key'])

        await StreamRepo(db).append(
            world_id, 'turn.committed',
            {'message_ids': [m['id'] for m in messages], 'turn_id': turn_id},
            request_id=request_id,
        )

        if snapshot.world.phase == Phase.PREPARED:
            await WorldRepo(db).set_phase(world_id, Phase.PLAYING)
        await WorldRepo(db).bump_version(world_id, draft.base_version, clock=new_clock)
    except RuntimeError as exc:
        if '版本冲突' in str(exc):
            raise Conflict('世界版本冲突', path='world.version') from exc
        raise

    if runtime is not None:
        notify_world(runtime, world_id)

    return CommittedTurn(
        turn_id=turn_id,
        request_id=request_id,
        version=draft.base_version + 1,
        messages=messages,
        public_outcome=result['public_outcome'],
        stop_reason='new_choice' if options else outcome.stop_reason,
    )
