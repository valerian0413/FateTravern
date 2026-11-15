"""Action turn state machine."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from pydantic import PrivateAttr

from app.application.flow import NarrativeFlow, listen, router, start
from app.application.commit import commit_turn
from app.application.entities import context_ids, relevant_ids, reconcile, duplicates
from app.application.context import build_public_context, build_world_context
from app.domain.changes import apply_to_copy, hash_inputs, register, stabilize_changes
from app.domain.errors import StaleTurn, ValidationError
from app.domain.stress import CAPACITIES, stress_boxes
from app.domain.types import (
    Channel,
    CandidateBatch,
    Intent,
    IntentKind,
    IssueSeverity,
    TurnDraft,
    TurnStage,
)
from app.api.notify import notify_world
from app.application.streaming import text_sink
from app.domain.inventory import currency_names, owned_items
from app.jobs import world_time
from app.modules import generation, narration, npc, planning, review, visibility
from app.rules.combat import absorptions, damage_effects
from app.rules.four_dice import FourDice
from app.storage.repositories import CheckRepo, EncounterRepo, MessageRepo, StreamRepo, TurnRepo, WorldRepo


@dataclass
class ActionCommand:
    world_id: str
    request_id: str
    turn_id: str
    text: str
    intent: Intent
    base_version: int


@dataclass
class TurnResult:
    turn_id: str
    request_id: str
    stage: str
    result: dict | None
    error: str | None = None


def choose_tools(intent: Intent, snapshot) -> dict:
    tools = set(intent.requested_tools or [])
    if intent.kind == IntentKind.EXPLORE:
        tools.update({'visibility', 'narration'})
        # Generation only fills gaps the intent module reported.
        if intent.unresolved:
            tools.add('generation')
        tools.add('planning')
        tools.discard('npc')
    if intent.kind == IntentKind.COCREATE:
        tools.update({'review'})
    return {'tools': sorted(tools), 'max_checks': 1}


def first_stop(plan: dict, upcoming_events: list) -> dict:
    """Limit one key check and one decision stop."""
    challenges = list(plan.get('challenges') or [])[:1]
    stop_at = None
    if upcoming_events:
        stop_at = upcoming_events[0].get('due_at')
    return {'challenges': challenges, 'stop_at': stop_at, 'stop_reason': plan.get('stop_reason', 'action_complete')}


def intent_checkpoint(intent: Intent) -> dict:
    return {
        'kind': intent.kind.value,
        'normalized_action': intent.normalized_action,
        'target_ids': intent.target_ids,
        'creation_statements': intent.creation_statements,
        'needs_clarification': intent.needs_clarification,
        'question': intent.question,
        'requested_tools': intent.requested_tools,
        'unresolved': intent.unresolved,
    }


class TurnService:
    def __init__(self, runtime):
        self.runtime = runtime
        self._locks: dict[str, asyncio.Lock] = {}

    def lock_for(self, world_id: str) -> asyncio.Lock:
        if world_id not in self._locks:
            self._locks[world_id] = asyncio.Lock()
        return self._locks[world_id]

    async def execute(self, command: ActionCommand) -> TurnResult:
        async with self.lock_for(command.world_id):
            turn = await TurnRepo(self.runtime.db.conn).get(command.world_id, command.turn_id)
            if turn and turn.stage == TurnStage.COMPLETED:
                return TurnResult(turn.id, turn.request_id, turn.stage.value, turn.result)
            encounter = await EncounterRepo(self.runtime.db.conn).get(command.world_id)
            if encounter and not encounter.get('result'):
                raise StaleTurn('场景已进入遭遇，请按当前轮重新声明行动')
            flow = ActionFlow(self.runtime, command, turn)
            return await flow.kickoff_async()

    async def resume(self, turn_id: str, world_id: str) -> TurnResult:
        turn = await TurnRepo(self.runtime.db.conn).get(world_id, turn_id)
        if not turn:
            raise ValidationError('回合不存在', path='turn_id')
        if turn.stage == TurnStage.COMPLETED:
            return TurnResult(turn.id, turn.request_id, turn.stage.value, turn.result)
        intent_data = turn.checkpoint.get('intent') or {}
        intent = Intent(
            kind=IntentKind(intent_data.get('kind', 'explore')),
            normalized_action=intent_data.get('normalized_action'),
            target_ids=list(intent_data.get('target_ids') or []),
            creation_statements=list(intent_data.get('creation_statements') or []),
            needs_clarification=bool(intent_data.get('needs_clarification')),
            question=intent_data.get('question'),
            requested_tools=list(intent_data.get('requested_tools') or []),
            unresolved=list(intent_data.get('unresolved') or []),
        )
        return await self.execute(ActionCommand(
            world_id=world_id, request_id=turn.request_id, turn_id=turn.id,
            text=turn.checkpoint.get('text', ''), intent=intent, base_version=turn.base_version,
        ))



class ActionFlow(NarrativeFlow):
    """Prepare, resolve and publish an action using the world's persisted checkpoints."""

    _runtime = PrivateAttr()
    _command = PrivateAttr()
    _turn = PrivateAttr()
    _snapshot = PrivateAttr()
    _action = PrivateAttr()
    _plan = PrivateAttr()
    _registered = PrivateAttr()
    _changes = PrivateAttr()
    _npc_ids = PrivateAttr()
    _npc_cards = PrivateAttr()
    _base_checkpoint = PrivateAttr()
    _resolution = PrivateAttr()
    _duration = PrivateAttr()
    _stop_reason = PrivateAttr()
    _check_id = PrivateAttr()
    _public = PrivateAttr()
    _grants = PrivateAttr()

    def __init__(self, runtime, command, turn):
        super().__init__(tracing=False, suppress_flow_events=True)
        self._runtime = runtime
        self._command = command
        self._turn = turn

    @start()
    async def prepare_action(self):
        command = self._command
        db = self._runtime.db.conn
        turns = TurnRepo(db)
        turn = self._turn
        snapshot = await WorldRepo(db).get_snapshot(command.world_id)
        if snapshot.world.version != command.base_version:
            raise StaleTurn('世界已推进，旧回合失效')

        history = await MessageRepo(db).recent(command.world_id, channel=None, limit=24)
        command.intent.target_ids = relevant_ids(snapshot, command.intent.normalized_action or command.text, command.intent.target_ids, history)

        # Every later checkpoint keeps text + intent so retry/resume can rebuild the command.
        base_checkpoint = {**(turn.checkpoint if turn else {}), 'text': command.text, 'intent': intent_checkpoint(command.intent)}
        await turns.checkpoint(command.world_id, command.turn_id, TurnStage.PREPARING, base_checkpoint)
        await StreamRepo(db).append(command.world_id, 'progress', {'stage': 'preparing'}, request_id=command.request_id)
        await db.commit()
        notify_world(self._runtime, command.world_id)

        tool_plan = choose_tools(command.intent, snapshot)
        action = command.intent.normalized_action or command.text
        batch = CandidateBatch([], [], [], [], [], [])
        combat_choice = base_checkpoint.get('combat_decision')
        prepared = base_checkpoint.get('prepared') or (combat_choice or {}).get('prepared')
        selected = base_checkpoint.get('selected_from')
        inherited = (selected or {}).get('registered')
        if prepared and duplicates(prepared.get('registered'), snapshot):
            existing = await CheckRepo(db).get(command.world_id, f'{command.world_id}:{command.request_id}:0')
            if existing and existing.get('dice'):
                raise StaleTurn('旧计划包含重复实体且已经掷骰，不能自动重新规划；请保留该记录并重新声明行动')
            # Automatic checks have no random outcome; invalidate only this failed turn's preparation.
            async with self._runtime.db.transaction():
                await db.execute('DELETE FROM checks WHERE world_id=? AND turn_id=?', (command.world_id, command.turn_id))
                for key in ('prepared','resolution','changes','issues','notice_sent','draft_ready'):
                    base_checkpoint.pop(key, None)
                base_checkpoint['replanned_reason'] = 'duplicate_entity_candidates'
                await turns.checkpoint(command.world_id, command.turn_id, TurnStage.PREPARING, base_checkpoint)
            prepared = None
        priority_ids = context_ids(snapshot, command.intent.normalized_action or command.text, command.intent.target_ids, history)
        if not prepared and not inherited and 'generation' in tool_plan['tools']:
            world_ctx = await build_world_context(snapshot, priority_ids, budget=30)
            batch = await generation.propose(self._runtime.provider, self._runtime.settings, {
                'world_id': command.world_id,
                'turn_id': command.turn_id,
                'action': action,
                'unresolved': command.intent.unresolved,
                'context': world_ctx,
                'force_batch': (self._runtime.fixtures or {}).get('generation'),
            })

        registered = (prepared or {}).get('registered') or (reconcile(inherited, snapshot, command.intent.target_ids) if inherited else register(reconcile(batch, snapshot, priority_ids[:30]), snapshot))

        # Materialize create_entity / append_fact changes from registered candidates.
        changes = list(registered.get('changes') or []) if self._runtime.provider is None else []
        npc_cards = {}
        for ent in registered.get('temp_entities') or []:
            changes.append({
                'op': 'create_entity', 'op_id': f"create-{ent['id']}", 'id': ent['id'],
                'kind': ent.get('kind', 'place'), 'name': ent.get('name', '未名'),
                'attributes': ent.get('attributes') or {},
            })
            if ent.get('kind') == 'npc':
                npc_cards[ent['id']] = ent
        for fact in registered.get('facts') or []:
            changes.append({
                'op': 'append_fact', 'op_id': f"fact-{fact.get('id')}", 'id': fact.get('id'),
                'subject_id': fact.get('subject_id'), 'predicate': fact.get('predicate', 'detail'),
                'value': fact.get('value'), 'disclosure_rule': fact.get('disclosure_rule'),
            })

        candidate_state = apply_to_copy(snapshot, changes, source_event_id='candidate')
        cards = npc.related_cards(candidate_state, command.intent.target_ids, action)
        history = await MessageRepo(db).recent(command.world_id, channel=None, limit=24)
        context = {
            'world_id': command.world_id, 'turn_id': command.turn_id, 'action': action,
            'world': await build_world_context(candidate_state, context_ids(candidate_state, action, command.intent.target_ids, history), budget=30),
            'public': build_public_context(candidate_state, [], [], action, 'planning').__dict__,
            'npc_cards': cards, 'skills': snapshot.entities[snapshot.world.player_id].attributes.get('skills', {}),
            'inventory': owned_items(candidate_state),
            'currency_denominations': currency_names(snapshot.world),
            'aspects': snapshot.entities[snapshot.world.player_id].attributes.get('aspects', []),
            'stress_boxes': stress_boxes(snapshot.entities[snapshot.world.player_id].attributes),
            'consequences': snapshot.entities[snapshot.world.player_id].attributes.get('consequences'),
            'rules': {k:v for k,v in snapshot.world.ruleset_config.items() if k!='world_book'},
            'history': [{'role': m['author'], 'text': m['text']} for m in history],
            'selected_from': {'prompt': selected.get('prompt'), 'options': selected.get('options')} if selected else None,
        }
        if self._runtime.provider is None:
            context['legacy_challenges'] = registered.get('challenges') or []
        plan = (prepared or {}).get('plan') or await planning.propose(
            self._runtime.provider, self._runtime.settings, context,
            fixture=(self._runtime.fixtures or {}).get('planning'), snapshot=candidate_state,
            allowed_npc_ids={c['entity_id'] for c in cards})
        plan = planning.validate_plan(plan, context)
        planning.validate_effects(plan, candidate_state, {c['entity_id'] for c in cards}, turn_id=command.turn_id)
        # Freeze entity IDs and plan before dice: retries never regenerate their inputs.
        base_checkpoint['prepared'] = {'registered': registered, 'plan': plan}
        await turns.checkpoint(command.world_id, command.turn_id, TurnStage.PREPARING, base_checkpoint)
        await db.commit()
        self._snapshot = snapshot
        self._action = action
        self._plan = plan
        self._registered = registered
        self._changes = changes
        self._npc_ids = {card['entity_id'] for card in cards}
        self._npc_cards = npc_cards
        self._base_checkpoint = base_checkpoint

    @router(prepare_action, emit=['await_player', 'resolve'])
    def route_plan(self):
        combat = self._plan['challenge'].get('combat') if self._plan['challenge']['mode'] == 'opposed' else None
        if combat and combat['defender_id'] == self._snapshot.world.player_id and not self._base_checkpoint.get('combat_decision'):
            return 'await_player'
        return 'await_player' if self._plan['choice_timing'] == 'before_action' and not self._base_checkpoint.get('combat_decision') else 'resolve'

    @listen('await_player')
    async def present_choices(self):
        plan = self._plan
        combat = plan['challenge'].get('combat') if plan['challenge']['mode'] == 'opposed' else None
        decision = None
        if combat and combat['defender_id'] == self._snapshot.world.player_id:
            options = ['进行防守检定', '在掷骰前退让，按自己的方式退出冲突']
            text = '\n'.join(filter(None, [plan['prompt'], plan['public_risk'], '对手即将攻击，你要防守，还是在掷骰前退让？']))
            decision = {'kind': 'before_roll'}
        else:
            options = planning.validate_options(plan['options'], required=True)
            text = '\n'.join(x for x in (plan['prompt'], planning.check_notice(plan), '你也可以自由描述其他行动。') if x)
        return await self._publish_decision(text, options, decision)

    async def _publish_decision(self, text, options, combat_decision):
        command = self._command
        db = self._runtime.db.conn
        async with self._runtime.db.transaction():
            msg = await MessageRepo(db).append(command.world_id, Channel.INTERACTION, 'gm', text,
                                               request_id=command.request_id, turn_id=command.turn_id)
            result = {'kind': 'decision', 'messages': [msg], 'options': options,
                      'prompt': text, 'base_version': self._snapshot.world.version}
            if combat_decision is not None:
                result['combat_decision'] = combat_decision
            await TurnRepo(db).checkpoint(command.world_id, command.turn_id, TurnStage.COMPLETED,
                                          self._base_checkpoint, result=result)
            await StreamRepo(db).append(command.world_id, 'interaction.completed',
                                       {'message_id': msg['id'], 'text': text}, request_id=command.request_id)
        notify_world(self._runtime, command.world_id)
        return TurnResult(command.turn_id, command.request_id, 'completed', result)

    @listen('resolve')
    async def resolve_action(self):
        command = self._command
        db = self._runtime.db.conn
        turns = TurnRepo(db)
        snapshot, plan = self._snapshot, self._plan
        base_checkpoint = self._base_checkpoint
        changes = self._changes
        notice = planning.check_notice(plan) if not base_checkpoint.get('combat_decision') else ''
        # Publish the check information before rolling, without an extra confirmation.
        if notice and not base_checkpoint.get('notice_sent'):
            async with self._runtime.db.transaction():
                msg = await MessageRepo(db).append(command.world_id, Channel.INTERACTION, 'gm', notice,
                                                   request_id=command.request_id, turn_id=command.turn_id)
                base_checkpoint['notice_sent'] = True
                await turns.checkpoint(command.world_id, command.turn_id, TurnStage.PREPARING, base_checkpoint)
                await StreamRepo(db).append(command.world_id, 'check.announced',
                                           {'message_id': msg['id'], 'text': notice}, request_id=command.request_id)
            notify_world(self._runtime, command.world_id)
        challenges = [plan['challenge']]

        due = world_time.due_events(snapshot, snapshot.world.clock + challenges[0].get('duration_minutes', 1))
        segment = first_stop({'challenges': challenges, 'stop_reason': 'action_complete'}, [
            {'due_at': e.due_at, 'id': e.id} for e in due
        ])
        challenge = segment['challenges'][0]
        duration = int(challenge.get('duration_minutes') or 1)
        if segment['stop_at'] is not None:
            duration = max(0, min(duration, segment['stop_at'] - snapshot.world.clock))
            if duration == 0:
                segment['stop_reason'] = 'unexpected_event'

        await turns.checkpoint(command.world_id, command.turn_id, TurnStage.RESOLVING, {
            **(self._turn.checkpoint if self._turn else {}),
            **base_checkpoint,
            'registered': {'id_map': self._registered.get('id_map') or {}, 'npc_cards': self._npc_cards},
            'changes': changes,
        })

        rules = FourDice(snapshot.world.ruleset_config)
        combat = challenge.get('combat') if challenge['mode'] == 'opposed' else None
        choice = base_checkpoint.get('combat_decision') or {}
        conceding = combat and choice.get('kind') == 'before_roll' and choice['choice'] == 1
        actor = snapshot.entities[combat['attacker_id'] if combat else snapshot.world.player_id]
        if conceding:
            assessment = rules.assess({'mode': 'automatic'}, actor.attributes)
        else:
            assessment = rules.assess(challenge, actor.attributes)
        check_id = f'{command.world_id}:{command.request_id}:0'
        assessment['check_id'] = check_id
        if combat and not conceding:
            defender = snapshot.entities[combat['defender_id']]
            defense_bonus = defender.attributes.get('skills', {}).get(combat['defense_domain'], 0)
            if type(defense_bonus) is not int:
                raise ValidationError('防守加值必须是整数')
            assessment['defense_bonus'] = defense_bonus

        existing_check = await CheckRepo(db).get(command.world_id, check_id)
        if conceding:
            resolution = rules.resolve(assessment)
        elif choice.get('kind') == 'after_hit':
            resolution = choice['resolution']
            check_id = resolution['check_id']
        elif existing_check:
            if existing_check['input_hash'] != hash_inputs({
                'challenge': assessment.get('challenge'),
                'actor': assessment.get('actor'),
                'config': {
                    'targets': rules.targets,
                    'environment_step': rules.environment_step,
                    'tie_succeeds': rules.tie_succeeds,
                },
            }):
                raise StaleTurn('检定输入已变化，不能复用旧骰点')
            resolution = rules.resolve(assessment, existing_check)
        else:
            resolution = rules.resolve(assessment, {'check_id': check_id})
            await CheckRepo(db).save(command.world_id, {
                'check_id': check_id,
                'turn_id': command.turn_id,
                'input_hash': resolution['input_hash'],
                'ruleset_version': resolution['ruleset_version'],
                'dice': resolution['dice'],
                'payload': resolution,
            })
            await self._runtime.db.conn.commit()

        if conceding:
            resolution['combat_event'] = '你在对手掷骰之前选择退让，退出当前冲突。'
            if choice.get('description'):
                resolution['combat_event'] += ' 你选择的退出方式：' + choice['description']
            self._action = choice.get('description') or '在掷骰前退让，退出当前冲突'
            changes.clear()
            changes.append({'op': 'concede', 'op_id': 'combat-concede', 'entity_id': snapshot.world.player_id})
        elif combat and resolution['damage']:
            damage = resolution['damage']
            legal = absorptions(defender.attributes, damage)
            if defender.id == snapshot.world.player_id and legal and choice.get('kind') != 'after_hit':
                offered = legal[:3]
                labels = []
                for box, slots, _ in offered:
                    parts = ([f'{box + 1} 点压力格'] if box is not None else [])
                    parts.extend(f'{CAPACITIES[slot]} 点伤痕' for slot in slots)
                    labels.append('承受' + '＋'.join(parts))
                labels.append('不吸收伤害，由对手决定出局后果')
                result = await self._publish_decision(
                    f"攻击骰 {resolution['dice']}（总值 {resolution['attack_total']}），"
                    f"防守骰 {resolution['defense_dice']}（总值 {resolution['defense_total']}）。"
                    f"你受到 {damage} 点伤害，请选择如何承受。",
                    labels, {'kind': 'after_hit', 'resolution': resolution, 'absorptions': offered},
                )
                return result
            index = choice.get('choice', 0) if choice.get('kind') == 'after_hit' else 0
            offered = choice['absorptions'] if choice.get('kind') == 'after_hit' else legal
            selected = offered[index] if index < len(offered) else None
            injury = choice.get('description') or combat['injury']
            changes.extend(damage_effects(defender.id, selected, injury))
            resolution['combat_event'] = (
                f'{defender.name}受到 {damage} 点伤害并出局。出局后果由对手决定，不等同于死亡。' if selected is None
                else f'{defender.name}受到 {damage} 点伤害，以压力和／或伤痕抵消。'
            )
            if selected and selected[1]:
                resolution['combat_event'] += ' 留下的伤痕：' + injury
        # Apply selected effects from resolution.
        for effect in resolution.get('selected_effects') or []:
            if isinstance(effect, dict) and effect.get('op'):
                if effect.get('op') == 'update_npc_card' and effect.get('entity_id') not in self._npc_ids:
                    raise ValidationError('不能修改无关 NPC 的角色卡', path='npc_card.entity_id')
                changes.append(effect)
        self._resolution = resolution
        self._duration = duration
        self._stop_reason = segment['stop_reason']
        self._check_id = check_id

    @listen(resolve_action)
    async def review_outcome(self, decision=None):
        if isinstance(decision, TurnResult):
            return decision
        command = self._command
        snapshot, plan = self._snapshot, self._plan
        registered, changes = self._registered, self._changes
        resolution = self._resolution
        base_checkpoint = self._base_checkpoint
        turns = TurnRepo(self._runtime.db.conn)
        # Disclosure
        known_fact_ids = [
            k.fact_id for k in snapshot.knowledge.values()
            if k.observer_id == snapshot.world.player_id and k.fact_id
        ]
        # Public facts count as known for prerequisites.
        for fact in snapshot.facts.values():
            if (fact.disclosure_rule or {}).get('disclosure') == 'public':
                known_fact_ids.append(fact.id)

        decisions = []
        # Evaluate existing hidden facts + newly proposed facts still only as candidates after apply preview.
        location_id = next((e.id for e in snapshot.entities.values() if e.kind.value == 'place'), None)
        for fact in list(snapshot.facts.values()) + [
            type('F', (), {
                'id': f.get('id'), 'disclosure_rule': f.get('disclosure_rule'),
            })() for f in registered.get('facts') or []
        ]:
            rule = getattr(fact, 'disclosure_rule', None)
            if not rule:
                continue
            decisions.append(visibility.evaluate_disclosure(
                rule, self._action,
                {'fact_id': fact.id, 'location_id': location_id, 'known_fact_ids': known_fact_ids},
                resolution,
            ))
        grants = visibility.issue_grants(decisions, command.turn_id, snapshot.world.player_id)
        for grant in grants:
            changes.append({
                'op': 'learn', 'op_id': f'learn-{grant.fact_id}', 'observer_id': grant.observer_id,
                'fact_id': grant.fact_id, 'basis': 'observed',
            })

        changes = stabilize_changes(changes, command.world_id, command.turn_id, existing_fact_ids=snapshot.facts)

        # Preview apply for public outcome assembly (without committing). Invalid changes fail here,
        # before narration, instead of surfacing only at commit.
        preview = apply_to_copy(snapshot, changes, source_event_id='preview')

        selected_events = plan['success_events' if resolution['success'] else 'failure_events']
        if resolution.get('combat_event'):
            selected_events = ([resolution['combat_event']] if resolution['mode'] == 'automatic'
                               else [*selected_events, resolution['combat_event']])
        public = build_public_context(
            preview, grants, [{'kind': 'resolution', 'success': resolution['success']},
                             *[{'kind': 'observable', 'text': text} for text in
                               selected_events]],
            authorized_action=self._action, stop_reason=self._stop_reason,
        )

        draft_dict = {
            'base_version': snapshot.world.version,
            'changes': changes,
            'public_outcome': public,
            'force_audit': False,
        }
        program_issues = review.validate_result(draft_dict, snapshot)
        if review.needs_semantic_audit(draft_dict):
            program_issues.extend(await review.semantic_audit(
                self._runtime.provider, self._runtime.settings,
                {'world_id': command.world_id, 'turn_id': command.turn_id, 'snapshot': snapshot, 'public': public},
                draft_dict,
            ))
        if any(i.severity == IssueSeverity.BLOCKING for i in program_issues):
            await turns.checkpoint(
                command.world_id, command.turn_id, TurnStage.FAILED_RETRYABLE,
                {**base_checkpoint, 'issues': [i.reason for i in program_issues], 'resolution': resolution},
                error_code='validation',
            )
            raise ValidationError(program_issues[0].reason, path=program_issues[0].path)
        self._public = public
        self._grants = grants
        self._changes = changes

    @listen(review_outcome)
    async def write_narrative(self, decision=None):
        if isinstance(decision, TurnResult):
            return decision
        command = self._command
        db = self._runtime.db.conn
        turns = TurnRepo(db)
        base_checkpoint, resolution = self._base_checkpoint, self._resolution
        plan, public = self._plan, self._public
        changes, grants = self._changes, self._grants
        await turns.checkpoint(command.world_id, command.turn_id, TurnStage.NARRATING, {
            **base_checkpoint, 'resolution': resolution, 'changes': changes, 'grants': [
                {'fact_id': g.fact_id, 'observer_id': g.observer_id} for g in grants
            ],
        })
        await StreamRepo(db).append(command.world_id, 'progress', {'stage': 'narrating'}, request_id=command.request_id)
        await db.commit()
        notify_world(self._runtime, command.world_id)

        narrative = await narration.compose(
            self._runtime.provider, self._runtime.settings, public,
            style={
                'world_id': command.world_id, 'turn_id': command.turn_id,
                'force_narration': (self._runtime.fixtures or {}).get('narration') is not None,
            },
            on_text=text_sink(self._runtime, command.world_id, 'narrative', command.request_id),
        )
        if (self._runtime.fixtures or {}).get('narration'):
            narrative = narration.narrative_text(self._runtime.fixtures['narration'])

        await turns.checkpoint(command.world_id, command.turn_id, TurnStage.VALIDATING, {
            **base_checkpoint, 'resolution': resolution, 'changes': changes, 'narration': narrative,
        })

        conceding = resolution['mode'] == 'automatic' and bool(resolution.get('combat_event'))
        after_action = plan['choice_timing'] == 'after_action' and not conceding
        check = {key: resolution[key] for key in ('mode', 'dice', 'skill_bonus', 'modifiers', 'total', 'dc', 'success')}
        if resolution['mode'] == 'opposed':
            check.update({key: resolution[key] for key in ('defense_dice', 'attack_total', 'defense_total', 'damage')})
        return TurnDraft(
            base_version=self._snapshot.world.version,
            registered_candidates={'id_map': self._registered.get('id_map') or {}, 'npc_cards': self._npc_cards},
            resolution_ids=[] if conceding else [self._check_id],
            changes=changes,
            events=[],
            public_outcome=public,
            narration=narrative,
            options=plan['options'] if after_action else [],
            choice_prompt=plan['prompt'] if after_action else '',
            check_result=check,
            next_jobs=[{
                'type': 'summarize',
                'dedup_key': f'summarize:{command.world_id}:{command.turn_id}',
                'payload': {'turn_id': command.turn_id},
            }],
        )

    @listen(write_narrative)
    async def publish_turn(self, draft: TurnDraft | TurnResult) -> TurnResult:
        if isinstance(draft, TurnResult):
            return draft
        command = self._command
        db = self._runtime.db.conn
        turns = TurnRepo(db)
        base_checkpoint, resolution = self._base_checkpoint, self._resolution
        await turns.checkpoint(command.world_id, command.turn_id, TurnStage.COMMITTING, {
            **base_checkpoint, 'resolution': resolution, 'draft_ready': True,
        })
        async with self._runtime.db.transaction():
            committed = await commit_turn(
                db, world_id=command.world_id, turn_id=command.turn_id,
                request_id=command.request_id, draft=draft, grants=self._grants,
                elapsed_minutes=self._duration, runtime=self._runtime,
            )
        return TurnResult(
            turn_id=command.turn_id,
            request_id=command.request_id,
            stage=TurnStage.COMPLETED.value,
            result={
                'messages': committed.messages,
                'public_outcome': committed.public_outcome,
                'stop_reason': committed.stop_reason,
                'resolution': {
                    'success': resolution['success'], 'dice': resolution['dice'],
                    'mode': resolution['mode'],
                },
            },
        )
