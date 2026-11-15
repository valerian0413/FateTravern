"""Run encounter decisions and commit the exact resulting state and visible events."""
from __future__ import annotations

import uuid
from copy import deepcopy

from app.api.notify import notify_world
from app.application.context import public_book
from app.application.streaming import text_sink
from app.domain.errors import StaleTurn, ValidationError
from app.domain.inventory import owned_items
from app.domain.stress import CAPACITIES, stress_boxes
from app.domain.types import Channel, EntityKind, PublicOutcome, TurnDraft, TurnStage
from app.modules import encounter as model
from app.modules import encounter_skills
from app.modules import narration
from app.rules.encounter import EncounterRound, ROUND_SECONDS, CONTESTED, GOAL_PROGRESS, OBJECTIVE
from app.storage.repositories import EncounterRepo, MessageRepo, StreamRepo, TurnRepo, WorldRepo
from app.application.commit import commit_turn


ROUND_PLAN_VERSION = 2


def active_encounter(state):
    return state if state and not state.get('result') else None


def public_encounter(state):
    if not state:
        return None
    player_id = state['order'][0]
    pending = state['units'][player_id].get('pending')
    return {
        'id': state['id'], 'kind': state['kind'], 'label': model.KINDS[state['kind']],
        'title': state['title'], 'scene': state['scene'], 'goal': state['goal'],
        'round': state['round'], 'elapsed_seconds': state['elapsed_seconds'],
        'round_seconds': ROUND_SECONDS, 'player_id': player_id,
        'progress': state['progress'] if state['kind'] != 'combat' or any(state['progress'].values()) else None,
        'goal_progress': GOAL_PROGRESS, 'result': state.get('result'),
        'waiting_for': 'absorption' if state.get('hit') else 'action',
        'units': [{key: state['units'][uid][key] for key in ('id', 'name', 'aspect', 'status', 'distance')}
                  for uid in state['order']],
        'scene_items': state['scene_items'],
        'pending': {'description': pending['action']['description'], 'remaining': pending['remaining']} if pending else None,
        'prepared': state['units'][player_id].get('prepared'),
    }


def unit(entity_id, name, aspect, habit, skills, distance):
    return {'id': entity_id, 'name': name, 'aspect': aspect, 'habit': habit,
            'skills': skills, 'distance': distance, 'status': 'active'}


def build_encounter(turn, snapshot, kind, previous, setup):
    if not isinstance(setup, dict) or type(setup.get('applicable')) is not bool:
        raise ValidationError('遭遇方案须说明是否存在短时对抗', path='encounter.applicable')
    if not setup['applicable']:
        return None, []
    rows = setup.get('opponents')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 4 or any(not isinstance(row, dict) for row in rows):
        raise ValidationError('遭遇须包含一至四个有效对手', path='encounter.opponents')

    player = snapshot.entities[snapshot.world.player_id]
    state = {
        'id': turn.id, 'kind': kind, 'title': model.short(setup.get('title'), 60, model.KINDS[kind]),
        'scene': model.short(setup.get('scene'), 120, '对抗在眼前展开。'),
        'scene_items': list(dict.fromkeys(
            item.strip()[:50] for item in setup.get('scene_items', [])[:8] if isinstance(item, str) and item.strip()
        )) if isinstance(setup.get('scene_items'), list) else [],
        'goal': model.short(setup.get('goal'), 100, '从冲突中脱身'),
        'opponent_goal': model.short(setup.get('opponent_goal'), 100, '迫使你退让'),
        'order': [player.id], 'units': {
            player.id: unit(player.id, player.name, '', '', model.skills(player.attributes.get('skills')), 'close'),
        },
        'round': 1, 'cursor': 0, 'elapsed_seconds': 0,
        'clock_remainder': previous['clock_remainder'] if previous else 0,
        'hit': None, 'progress': {'player': 0, 'opponents': 0},
        'starting_scars': [slot for slot, scar in player.attributes.get('consequences', {}).items() if scar.get('text')],
    }
    changes = []
    names = set()
    for index, row in enumerate(rows):
        name = model.short(row.get('name'), 50)
        eid = model.short(row.get('entity_id'), 100)
        entity = snapshot.entities.get(eid)
        if entity and entity.kind != EntityKind.NPC and name == entity.name:
            raise ValidationError('遭遇对手必须引用现有 NPC，或提供新对手名字', path='encounter.entity_id')
        if not entity or entity.kind != EntityKind.NPC:
            # A borrowed location ID or an invented ID is not an opponent identity.
            matches = [e for e in snapshot.entities.values() if name and e.kind == EntityKind.NPC
                       and (name == e.name or name in (e.attributes.get('aliases') or []))]
            if len(matches) > 1:
                raise ValidationError('有多个同名对手，请提供已存在的对手 ID', path='encounter.entity_id')
            entity = matches[0] if matches else None
            eid = ''
        if entity:
            eid, name = entity.id, entity.name
            if entity.attributes.get('combat_status') == 'taken_out':
                raise ValidationError('已出局的对手无法发起新的遭遇', path='encounter.entity_id')
        if not name or name in names or eid in state['units']:
            raise ValidationError('对手缺少名称或重复出现', path='encounter.opponents')
        names.add(name)
        aspect = model.short(row.get('aspect'), 100, name)
        habit = model.short(row.get('habit'), 150, '面对眼前威胁，危险增大时撤离')
        supplied = model.skills(row.get('skills'))
        if entity:
            supplied.update(model.skills(entity.attributes.get('skills')))
            habit = model.short(entity.attributes.get('encounter_habit'), 150, habit)
            card = snapshot.npc_cards.get(eid)
            if card:
                habit = model.short('、'.join([*card.traits, *card.goals, habit]), 150, habit)
        else:
            eid = str(uuid.uuid5(uuid.UUID(turn.id), f'opponent-{index}'))
            changes.extend([
                {'op': 'create_entity', 'op_id': f'encounter-opponent-{index}', 'id': eid, 'kind': 'npc', 'name': name,
                 'attributes': {'description': aspect, 'skills': supplied, 'encounter_habit': habit}},
                {'op': 'append_fact', 'op_id': f'encounter-identity-{index}', 'subject_id': eid, 'predicate': 'identity',
                 'value': aspect, 'disclosure_rule': {'disclosure': 'public'}},
            ])
        state['order'].append(eid)
        state['units'][eid] = unit(eid, name, aspect, habit, supplied,
                                  'far' if row.get('distance') == 'far' else 'close')
    return state, changes


def plan_round(raw, state):
    if not isinstance(raw, dict) or not isinstance(raw.get('opponents'), dict):
        raise ValidationError('遭遇计划须包含玩家与对手动作', path='encounter.round')
    if isinstance(raw.get('player'), list):
        steps = raw['player']
        if not 1 <= len(steps) <= 3 or any(not isinstance(step, dict) for step in steps):
            raise ValidationError('每轮可安排一至三个连续步骤', path='encounter.steps')
        durations = [step.get('seconds') for step in steps]
        if any(type(seconds) is not int or not 1 <= seconds <= ROUND_SECONDS for seconds in durations) or sum(durations) > ROUND_SECONDS:
            raise ValidationError('每个步骤须标明 seconds，整轮合计不能超过8秒', path='encounter.steps')
        if sum(step.get('kind') == 'attack' for step in steps) > 1:
            raise ValidationError('一轮最多一次主要攻击，不能用拆步骤增加攻击次数', path='encounter.steps')
        planned_steps = []
        for index, step in enumerate(steps):
            dependent = step.get('requires_previous_success', index > 0)
            if type(dependent) is not bool:
                raise ValidationError('步骤依赖须为布尔值', path='encounter.steps')
            move = plan_round({**raw, 'player': step}, state)[state['order'][0]]
            if len(steps) > 1 and move['kind'] in ('clarify', 'continue', 'withdraw'):
                raise ValidationError('澄清、继续跨轮准备或撤离须单独执行', path='encounter.steps')
            if move['kind'] == 'prepare' and move['rounds'] > 1 and index < len(steps) - 1:
                raise ValidationError('跨轮准备尚未完成，后续动作应留待下一轮', path='encounter.steps')
            move.update(seconds=durations[index], requires_previous_success=dependent)
            planned_steps.append(move)
        planned = plan_round({**raw, 'player': steps[0]}, state)
        planned[state['order'][0]] = {**planned_steps[0], 'followups': planned_steps[1:]}
        return planned
    player_id = state['order'][0]
    player_move = model.action(raw.get('player'))
    if player_move['kind'] == 'clarify':
        return {player_id: player_move}
    targets = [uid for uid in state['order'][1:] if state['units'][uid]['status'] == 'active']
    planned = {}
    for uid in [player_id, *targets]:
        move = player_move if uid == player_id else model.action(raw['opponents'].get(uid))
        if move['kind'] == 'attack' and state['kind'] != 'combat':
            move['kind'] = 'advance'
        if (state['kind'] == 'combat' and uid != player_id and move['target_id'] == OBJECTIVE
                and move['kind'] in ('attack', 'advance')):
            move['kind'] = 'advance'
            planned[uid] = move
            continue
        if move['kind'] in CONTESTED | {'approach', 'study'}:
            possible = targets if uid == player_id else [player_id]
            if uid == player_id and move['kind'] == 'advance' and move['target_id'] == OBJECTIVE:
                if len(possible) != 1:
                    raise ValidationError('推进场景目标时，请指定阻碍行动的具体对手 ID', path='encounter.target_id')
                move['target_id'] = possible[0]
            if not move['target_id'] and len(possible) == 1:
                move['target_id'] = possible[0]
            if move['target_id'] not in possible:
                raise ValidationError('动作需指定仍在场的有效对手', path='encounter.target_id')
        if move['kind'] == 'study' and not move['weakness']:
            raise ValidationError('寻找弱点须说明成功时能发现的具体破绽', path='encounter.weakness')
        if move['kind'] == 'search' and not set(move['search_items']) <= set(state['scene_items']):
            raise ValidationError('搜索结果只能引用当前场景已有的物品名称', path='encounter.search_items')
        if uid != player_id and move['kind'] == 'clarify':
            raise ValidationError('对手行动不能请求澄清', path='encounter.action')
        if move['kind'] == 'continue' and not state['units'][uid].get('pending'):
            raise ValidationError('没有可以继续的准备', path='encounter.action')
        planned[uid] = move
    return planned


def summary_events(state, game, is_start):
    if is_start:
        game.tell(state['scene'])
        for uid in state['order'][1:]:
            opponent = state['units'][uid]
            game.tell(f"{opponent['name']} · {opponent['aspect']}。")


def hit_prompt(hit):
    options = []
    labels = {'mild': '轻微', 'moderate': '中度', 'severe': '严重'}
    for box, slots, _ in hit['choices']:
        parts = [f'{box + 1} 点压力格'] if box is not None else []
        parts.extend(f'{CAPACITIES[slot]} 点伤痕（{labels[slot]}）' for slot in slots)
        options.append('承受' + '＋'.join(parts))
    options.append('不吸收伤害，退出当前遭遇')
    attacker = f"{hit['attacker']}的攻击命中了你。" if hit.get('attacker') else ''
    return f"{attacker}你受到 {hit['damage']} 点伤害。请选择如何承受。", options


class EncounterService:
    def __init__(self, runtime, turn_service):
        self.runtime = runtime
        self.turn_service = turn_service

    async def reply(self, turn, question):
        db = self.runtime.db.conn
        async with self.runtime.db.transaction():
            turns = TurnRepo(db)
            current = await turns.get(turn.world_id, turn.id)
            if current.stage == TurnStage.CANCELLED:
                return
            message = await MessageRepo(db).append(turn.world_id, Channel.INTERACTION, 'gm', question,
                                                  request_id=turn.request_id, turn_id=turn.id)
            await turns.checkpoint(turn.world_id, turn.id, TurnStage.COMPLETED, {},
                                          result={'kind': 'clarify', 'messages': [message]})
            await StreamRepo(db).append(turn.world_id, 'interaction.completed',
                                        {'message_id': message['id'], 'text': question}, request_id=turn.request_id)
        notify_world(self.runtime, turn.world_id)

    async def execute(self, turn, route):
        async with self.turn_service.lock_for(turn.world_id):
            db = self.runtime.db.conn
            turns = TurnRepo(db)
            turn = await turns.get(turn.world_id, turn.id)
            if turn.stage in (TurnStage.COMPLETED, TurnStage.CANCELLED, TurnStage.STALE):
                return
            snapshot = await WorldRepo(db).get_snapshot(turn.world_id)
            if turn.base_version != snapshot.world.version:
                raise StaleTurn('场景已更新，请查看当前轮并重新声明行动')
            checkpoint = {**turn.checkpoint, 'encounter_route': route}
            if 'encounter_route' not in turn.checkpoint:
                await self.save_checkpoint(turn, checkpoint)
            previous = await EncounterRepo(db).get(turn.world_id)
            state = active_encounter(previous)
            start = state is None
            changes = []
            if start:
                if route['kind'] not in model.KINDS:
                    raise StaleTurn('当前遭遇已经结束')
                setup = checkpoint.get('encounter_setup')
                if setup is None:
                    payload = {'kind': route['kind'], 'action': route['action'],
                               'player_text': turn.checkpoint['text'],
                               'target_ids': [eid for eid in route['target_ids'] if eid in snapshot.entities
                                              and snapshot.entities[eid].kind == EntityKind.NPC],
                               'context': route['context']}
                    try:
                        setup = await model.ask(self.runtime, turn, 'encounter_setup', payload, {'applicable': False})
                        state, changes = build_encounter(turn, snapshot, route['kind'], previous, setup)
                    except ValidationError as exc:
                        setup = await model.ask(self.runtime, turn, 'encounter_setup',
                                                {**payload, 'correction': exc.message}, {'applicable': False})
                        state, changes = build_encounter(turn, snapshot, route['kind'], previous, setup)
                else:
                    state, changes = build_encounter(turn, snapshot, route['kind'], previous, setup)
                if not state:
                    await self.reply(turn, '当前还没有能展开多轮对抗的对手或紧迫目标。你想先接近目标、调查，还是说明要对付谁？')
                    return
                if 'encounter_setup' not in checkpoint:
                    checkpoint['encounter_setup'] = setup
                    await self.save_checkpoint(turn, checkpoint)
            else:
                if route['kind']:
                    raise StaleTurn('当前已有遭遇，请针对当前轮声明行动')
                state = deepcopy(state)
            if (cached := checkpoint.get('encounter_result')) is None:
                cached = await self.resolve(turn, snapshot, state, start, changes, route, checkpoint)
                if cached is None:
                    return
                checkpoint['encounter_result'] = cached
                await self.save_checkpoint(turn, checkpoint)
            await self.commit(turn, snapshot, cached, route)

    async def save_checkpoint(self, turn, checkpoint, stage=TurnStage.RESOLVING):
        async with self.runtime.db.transaction():
            turns = TurnRepo(self.runtime.db.conn)
            current = await turns.get(turn.world_id, turn.id)
            if current.stage == TurnStage.CANCELLED:
                raise StaleTurn('遭遇行动已取消')
            await turns.checkpoint(
                turn.world_id, turn.id, stage, checkpoint,
            )

    async def resolve(self, turn, snapshot, state, start, changes, route, checkpoint):
        events, checks = [], []
        game = EncounterRound(snapshot, state, changes, events, checks, turn.request_id)
        summary_events(state, game, start)
        player_id = snapshot.world.player_id
        choice = turn.checkpoint.get('combat_decision') or {}
        if hit := state.get('hit'):
            if choice.get('kind') != 'encounter_hit' or choice.get('encounter_id') != state['id']:
                raise StaleTurn('请先选择如何承受当前攻击')
            selection = hit['choices'][choice['choice']] if choice['choice'] < len(hit['choices']) else None
            game.injury(player_id, hit['damage'], choice.get('description') or hit['injury'], selection)
            state['hit'] = None
        else:
            if choice.get('kind') == 'encounter_hit':
                raise StaleTurn('这次受击已处理')
            payload = {
                'encounter': public_encounter(state),
                'player_text': turn.checkpoint['text'], 'action': route['action'],
                'opponent_goal': state['opponent_goal'], 'order': state['order'],
                'participants': [
                    {**state['units'][uid], 'pressure': stress_boxes(game.attributes(uid)),
                     'consequences': {slot: data.get('text') for slot, data in
                                      game.attributes(uid).get('consequences', {}).items()}}
                    for uid in state['order'] if state['units'][uid]['status'] == 'active'
                ],
                'inventory': [item['name'] for item in owned_items(snapshot)],
                'scene_items': state['scene_items'],
                'recent_events': state.get('recent_events', []),
            }
            if self.runtime.provider is None and not (self.runtime.fixtures or {}).get('encounter_round'):
                raise ValidationError('恢复模型连接后即可继续当前遭遇')
            if checkpoint.get('encounter_plan_version') != ROUND_PLAN_VERSION or 'encounter_plan' not in checkpoint:
                try:
                    raw = await model.ask(self.runtime, turn, 'encounter_round', payload, None)
                    checkpoint['encounter_plan'] = plan_round(raw, state)
                except ValidationError as exc:
                    raw = await model.ask(self.runtime, turn, 'encounter_round',
                                          {**payload, 'correction': exc.message}, None)
                    checkpoint['encounter_plan'] = plan_round(raw, state)
                checkpoint['encounter_plan_version'] = ROUND_PLAN_VERSION
                await self.save_checkpoint(turn, checkpoint)
            state['planned'] = deepcopy(checkpoint['encounter_plan'])
            card = game.attributes(player_id)
            sections = (public_book(snapshot) or {}).get('sections', {})
            checks_plan, question = await encounter_skills.select(self.runtime, turn, {
                **payload,
                'player_training': {key: card[key] for key in ('background', 'identity', 'skill_reason', 'aspects')
                                    if key in card},
                'supernatural': [entry['text'] for entry in sections.get('supernatural', [])],
            }, state['planned'])
            for key, uid, move in model.planned_actions(state['planned']):
                move.update(checks_plan.get(key, {}))
            player_move = state['planned'][player_id]
            if player_move['kind'] == 'clarify':
                question = player_move['description']
            if (not question and player_move['kind'] == 'attack' and player_move['reach'] == 'close'
                    and state['units'][player_move['target_id']]['distance'] == 'far'):
                question = '目前的手段需要接触目标。你想怎样接近，或是改用可用的远程手段？'
            if question:
                if start:
                    state.pop('planned')
                    return {'state': state, 'events': events, 'changes': changes, 'checks': [],
                            'minutes': 0, 'question': question}
                await self.reply(turn, question)
                return None

        while not state.get('hit') and state['cursor'] < len(state['order']):
            if game.finish():
                break
            uid = state['order'][state['cursor']]
            state['cursor'] += 1
            if uid in state['planned']:
                game.perform_sequence(uid, state['planned'][uid])
        if not state.get('hit'):
            if not state.get('result'):
                game.finish()
            state['cursor'] = 0
            state.pop('planned')
            state['elapsed_seconds'] += ROUND_SECONDS
            minutes, state['clock_remainder'] = divmod(state['clock_remainder'] + ROUND_SECONDS, 60)
            if not state.get('result'):
                state['round'] += 1
        else:
            minutes = 0
        if events:
            recent = state.get('recent_events', []) if choice else []
            state['recent_events'] = (recent + [event['text'] for event in events])[-8:]
        return {'state': state, 'events': events, 'changes': changes, 'checks': checks, 'minutes': minutes}

    async def commit(self, turn, snapshot, cached, route):
        state = cached['state']
        active = not state.get('result')
        hit = state.get('hit')
        decision = None
        options = []
        prompt = ''
        if hit:
            prompt, options = hit_prompt(hit)
            decision = {'kind': 'encounter_hit', 'encounter_id': state['id']}
        elif active:
            pending = state['units'][snapshot.world.player_id].get('pending')
            if pending:
                prompt = f"第 {state['round']} 轮轮到你。「{pending['action']['description']}」还需 {pending['remaining']} 轮。"
                options = ['继续当前准备', '放弃准备，专注防守']
            else:
                prompt = cached.get('question') or f"第 {state['round']} 轮轮到你。你想做什么？"
                options = ['我专注防守', '我观察对手的动向']
                if state['kind'] == 'combat' and state['scene_items']:
                    options.append('我查看手边有哪些用得上的东西')
                if state['kind'] == 'combat':
                    nearby = any(state['units'][uid]['distance'] == 'close' and state['units'][uid]['status'] == 'active'
                                 for uid in state['order'][1:])
                    if nearby:
                        options.append('我拉开距离，留在交战范围内')
                else:
                    options.append('我尝试退出当前对抗')
        changes = list(cached['changes'])
        if state.get('result') in ('player_goal', 'opponents_goal'):
            side = 'goal' if state['result'] == 'player_goal' else 'opponent_goal'
            changes.append({
                'op': 'append_fact', 'op_id': 'encounter-goal', 'subject_id': snapshot.world.player_id,
                'predicate': 'encounter_outcome',
                'value': {'scene': state['title'], 'kind': state['kind'], 'winner': state['result'],
                          'achieved': state[side]},
                'disclosure_rule': {'disclosure': 'public'},
            })
        public = public_encounter(state)
        player_text = turn.checkpoint.get('text') or route['action']
        outcome = PublicOutcome(
            authorized_action=player_text, observable_events=cached['events'],
            visible_entities=public['units'], visible_facts=[],
            stop_reason='encounter_ended' if not active else 'encounter_waiting',
        )
        current = await TurnRepo(self.runtime.db.conn).get(turn.world_id, turn.id)
        narrative = current.checkpoint.get('encounter_narration')
        if narrative is None:
            await self.save_checkpoint(turn, current.checkpoint, TurnStage.NARRATING)
            await StreamRepo(self.runtime.db.conn).append(
                turn.world_id, 'progress', {'stage': 'narrating'}, request_id=turn.request_id,
            )
            await self.runtime.db.conn.commit()
            notify_world(self.runtime, turn.world_id)
            fixture = (self.runtime.fixtures or {}).get('narration')
            narrative = await narration.compose(
                self.runtime.provider, self.runtime.settings, outcome,
                style={
                    'world_id': turn.world_id, 'turn_id': turn.id,
                    'force_narration': fixture is not None,
                    'encounter': public,
                    'resolved_checks': cached['checks'],
                    'pending_damage': ({'target_id': hit['target_id'], 'damage': hit['damage'],
                                        'attacker': hit['attacker']} if hit else None),
                    'max_chars': self.runtime.settings.narration_max_chars,
                },
                on_text=text_sink(self.runtime, turn.world_id, 'narrative', turn.request_id),
            )
            if fixture is not None:
                narrative = narration.narrative_text(fixture)
            await self.save_checkpoint(
                turn, {**current.checkpoint, 'encounter_narration': narrative}, TurnStage.VALIDATING,
            )
        draft = TurnDraft(
            base_version=snapshot.world.version, registered_candidates={},
            resolution_ids=[r['check_id'] for r in cached['checks']],
            changes=changes, events=[], public_outcome=outcome, options=options,
            choice_prompt=prompt, encounter=public, combat_decision=decision,
            encounter_checks=cached['checks'],
            narration=narrative,
            next_jobs=[{'type': 'summarize', 'payload': {'turn_id': turn.id},
                        'dedup_key': f'encounter-summary:{state["id"]}'}] if not active else [],
        )
        db = self.runtime.db.conn
        async with self.runtime.db.transaction():
            current = await TurnRepo(db).get(turn.world_id, turn.id)
            if current.stage == TurnStage.CANCELLED:
                return
            await commit_turn(db, world_id=turn.world_id, turn_id=turn.id, request_id=turn.request_id,
                              draft=draft, elapsed_minutes=cached['minutes'])
            await EncounterRepo(db).save(turn.world_id, state)
        self.runtime.trace.write({
            'event': 'encounter.committed', 'world_id': turn.world_id, 'turn_id': turn.id,
            'request_id': turn.request_id, 'encounter': public,
            'checks': cached['checks'], 'observable_events': cached['events'],
        })
        notify_world(self.runtime, turn.world_id)
