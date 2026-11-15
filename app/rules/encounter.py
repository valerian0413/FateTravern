"""Deterministic action order and effects for one encounter."""
from __future__ import annotations

from app.rules.combat import absorptions, damage_effects, incapacitating_consequence
from app.rules.four_dice import FourDice

ROUND_SECONDS = 8
GOAL_PROGRESS = 3
CONTESTED = {'attack', 'advance', 'withdraw'}
OBJECTIVE = 'objective'


class EncounterRound:
    def __init__(self, snapshot, state, changes, events, checks, request_id):
        self.snapshot = snapshot
        self.state = state
        self.changes = changes
        self.events = events
        self.checks = checks
        self.request_id = request_id
        self.rules = FourDice(snapshot.world.ruleset_config)
        self.fate = snapshot.world.ruleset_id == 'fate-lite-v1'
        self.player_id = snapshot.world.player_id

    def tell(self, text):
        self.events.append({'kind': 'observable', 'text': text})

    def attributes(self, unit_id):
        entity = self.snapshot.entities.get(unit_id)
        if entity:
            return entity.attributes
        return {'skills': self.state['units'][unit_id]['skills']}

    def roll(self, actor_id, action, target_id=None):
        actor = self.state['units'][actor_id]
        target = self.state['units'].get(target_id)
        challenge = {'mode': 'opposed' if target else 'check', 'goal': action['description'],
                     'domain': action['skill'], 'difficulty': action['difficulty'], 'conditions': []}
        if target:
            challenge.update(attacker_id=actor_id, defender_id=target_id, defense_domain=action['defense_skill'])
        prepared_target = actor.get('prepared_target')
        if (action['use_preparation'] and actor.get('prepared')
                and (not prepared_target or (action['kind'] == 'attack' and prepared_target == target_id))):
            preparation = actor.pop('prepared')
            actor.pop('prepared_target', None)
            challenge['conditions'].append({'source_id': 'encounter:preparation', 'direction': 'advantage',
                                            'public': True, 'reason': preparation})
            self.tell(f"{actor['name']}利用已经完成的准备「{preparation}」。")
        assessment = self.rules.assess(challenge, {'skills': actor['skills']})
        if target:
            assessment['defense_bonus'] = (
                target['skills'].get(action['defense_skill'], 0) + int(bool(target.get('guard')))
            )
            challenge['defense_bonus'] = assessment['defense_bonus']
        check_id = f"{self.snapshot.world.id}:{self.request_id}:{len(self.checks)}"
        result = self.rules.resolve(assessment, {'check_id': check_id})
        decision = {key: action[key] for key in ('kind', 'description', 'skill', 'reach', 'equipment', 'skill_reason')
                    if key in action}
        actor['skill_history'] = [*actor.get('skill_history', [])[-2:], decision]
        self.checks.append({**result, 'actor': actor['name'], 'target': target['name'] if target else None,
                            'purpose': action['kind'],
                            'skill_reason': action.get('skill_reason'),
                            'defense_domain': action['defense_skill'] if target else None,
                            'defense_bonus': assessment.get('defense_bonus')})
        return result

    def injury(self, target_id, damage, injury, selection):
        target = self.state['units'][target_id]
        if selection is None:
            target['status'] = 'out'
        if self.fate:
            if selection and selection[1] and incapacitating_consequence(injury):
                injury = '受创后动作受限'
            for effect in damage_effects(target_id, selection, injury):
                if effect['op'] != 'taken_out' or target_id != self.player_id:
                    effect['op_id'] += f'-{len(self.changes)}'
                    self.changes.append(effect)
        else:
            target['wear'] = target.get('wear', 0) + damage
            if target['wear'] >= 3:
                target['status'] = 'out'
        if target['status'] == 'out':
            self.tell(f"{target['name']}因这次攻击退出当前遭遇。")
        elif self.fate:
            self.tell(f"{target['name']}抵消了 {damage} 点伤害。" + (f"留下伤痕「{injury}」。" if selection[1] else ''))
        else:
            self.tell(f"{target['name']}承受了 {damage} 点压力。")

    def current_attributes(self, target_id):
        # Earlier units in this round may already have filled the defender's boxes.
        from app.domain.changes import apply_to_copy
        return apply_to_copy(self.snapshot, self.changes).entities[target_id].attributes

    def perform_sequence(self, actor_id, first):
        previous_succeeded = True
        for index, step in enumerate([first, *first.get('followups', [])]):
            if self.state.get('hit') or self.state.get('result'):
                break
            if index and not previous_succeeded and step.get('requires_previous_success', True):
                self.tell(f"前一步未达成，{self.state['units'][actor_id]['name']}未执行后续动作「{step['description']}」。")
                continue
            count = len(self.checks)
            executed = self.perform(actor_id, {k: v for k, v in step.items() if k != 'followups'})
            previous_succeeded = (self.checks[-1]['success'] if len(self.checks) > count else executed is not False)
            if self.state['units'][actor_id].get('pending'):
                break
            if index < len(first.get('followups', [])) and self.finish():
                break

    def perform(self, actor_id, action):
        state = self.state
        unit = state['units'][actor_id]
        kind = action['kind']
        is_player = actor_id == self.player_id
        if unit['status'] != 'active':
            return False
        unit.pop('guard', None)
        if kind != 'continue' and unit.pop('pending', None):
            self.tell(f"{unit['name']}中断了原来的准备。")
        if kind in ('prepare', 'continue'):
            pending = unit.get('pending')
            if kind == 'prepare':
                pending = unit['pending'] = {'action': action, 'remaining': action['rounds']}
            if not pending:
                self.tell(f"{unit['name']}没有需要继续的准备。")
                return False
            pending['remaining'] -= 1
            detail = pending['action']['description']
            if pending['remaining']:
                verb = '开始' if kind == 'prepare' else '继续'
                self.tell(f"{unit['name']}{verb}准备「{detail}」，还需 {pending['remaining']} 轮。")
            else:
                unit.pop('pending')
                if self.roll(actor_id, pending['action'])['success']:
                    unit['prepared'] = detail
                    unit.pop('prepared_target', None)
                    self.tell(f"{unit['name']}完成准备「{detail}」。实际用到时获得一次情境加值。")
                else:
                    self.tell(f"{unit['name']}未能完成{detail}。")
            return

        objective = action['target_id'] == OBJECTIVE
        target_id = self.player_id if objective else action['target_id']
        target = state['units'].get(target_id)
        if kind == 'guard':
            unit['guard'] = True
            self.tell(f"{unit['name']}专注防守，直到下次行动前防守 +1。")
        elif kind == 'wait':
            self.tell(f"{unit['name']}选择等待：{action['description']}。")
        elif kind == 'observe':
            self.tell(f"{unit['name']}观察眼前局势。")
            if is_player and state['scene_items']:
                self.tell('你能看到：' + '、'.join(state['scene_items']) + '。')
        elif kind == 'search':
            if self.roll(actor_id, action)['success']:
                self.tell(f"{unit['name']}及时辨认出能派上用场的物品：" + '、'.join(action['search_items']) + '。')
            else:
                self.tell(f"{unit['name']}没能在这轮辨认出哪些物品能派上用场。")
        elif kind == 'study' and target and target['status'] == 'active':
            if self.roll(actor_id, action)['success']:
                weakness = action['weakness']
                unit.update(prepared=weakness, prepared_target=target_id)
                self.tell(f"{unit['name']}发现{target['name']}的破绽「{weakness}」。利用它攻击该目标时可获得一次情境加值。")
            else:
                self.tell(f"{unit['name']}没能看清{target['name']}的破绽。")
        elif kind == 'reposition':
            if is_player:
                for uid in state['order'][1:]:
                    state['units'][uid]['distance'] = 'far'
            else:
                unit['distance'] = 'far'
            self.tell(f"{unit['name']}拉开了距离。")
        elif kind == 'approach' and target and target['status'] == 'active':
            relative = target if is_player else unit
            if relative['distance'] == 'far':
                relative['distance'] = 'close'
                self.tell(f"{unit['name']}接近了{target['name']}。")
            else:
                self.tell(f"{unit['name']}仍贴近{target['name']}。")
        elif kind == 'withdraw' and state['kind'] in ('combat', 'negotiation'):
            unit['status'] = 'withdrawn'
            if is_player and self.fate and state['kind'] == 'combat':
                scars = self.attributes(actor_id).get('consequences', {})
                slots = [slot for slot, scar in scars.items()
                         if scar.get('text') and slot not in self.state['starting_scars']]
                self.changes.append({'op': 'encounter_concede', 'op_id': 'encounter-concede',
                                     'entity_id': actor_id, 'scar_slots': slots})
                self.tell(f"{unit['name']}在对手掷骰前退让，获得 {1 + len(slots)} 个命运点。")
            else:
                self.tell(f"{unit['name']}退出了当前遭遇。")
        elif kind in CONTESTED and target and target['status'] == 'active':
            distance = (target if is_player else unit)['distance']
            if (kind == 'attack' or objective) and distance == 'far':
                # A saved mid-round plan may predate the explicit reach field.
                reach = action.get('reach', 'far' if action['skill'] == '射击' else 'close')
                if reach != 'far':
                    self.tell(f"{unit['name']}距离过远，这次攻击未能出手。")
                    return False
            self.tell(f"{unit['name']}尝试{action['description']}。")
            result = self.roll(actor_id, action, target_id)
            damage = result['damage']
            if not damage:
                self.tell(f"{target['name']}挡下了这次行动。")
            elif kind == 'attack':
                if not self.fate:
                    self.injury(target_id, damage, action['injury'], True)
                    return
                choices = absorptions(self.current_attributes(target_id), damage)
                if target_id == self.player_id:
                    by_consequence = {}
                    for choice in choices:
                        by_consequence.setdefault(choice[1], choice)
                    state['hit'] = {'target_id': target_id, 'damage': damage, 'injury': action['injury'],
                                    'attacker': unit['name'], 'choices': list(by_consequence.values())[:3]}
                    return
                self.injury(target_id, damage, action['injury'], choices[0] if choices else None)
            elif kind == 'withdraw' and is_player:
                unit['status'] = 'withdrawn'
                self.tell(f"{unit['name']}成功脱离追逐。")
            else:
                side = 'player' if is_player else 'opponents'
                state['progress'][side] = min(GOAL_PROGRESS, state['progress'][side] + min(damage, 2))
                goal = state['goal'] if is_player else state['opponent_goal']
                self.tell(f"{unit['name']}向目标「{goal}」推进至 {state['progress'][side]}/{GOAL_PROGRESS}。")
        elif kind in CONTESTED | {'approach', 'study'}:
            self.tell(f"{unit['name']}的目标已经离场，这次行动没有执行。")
            return False

    def finish(self):
        state = self.state
        if state.get('result'):
            return True
        player = state['units'][self.player_id]
        if player['status'] != 'active':
            state['result'] = 'player_out' if player['status'] == 'out' else 'player_withdrew'
            self.tell('你已离开当前遭遇。')
            if state['kind'] == 'combat':
                self.tell(f"你的目标「{state['goal']}」尚未达成。")
        elif not any(u['status'] == 'active' for uid, u in state['units'].items() if uid != self.player_id):
            state['result'] = 'opponents_left'
            self.tell('对手已退出当前遭遇。')
        elif state['progress']['player'] >= GOAL_PROGRESS:
            state['result'] = 'player_goal'
            self.tell(f"你在这次对抗中达成目标：{state['goal']}。")
        elif state['progress']['opponents'] >= GOAL_PROGRESS:
            state['result'] = 'opponents_goal'
            self.tell(f"对手在这次对抗中达成目标：{state['opponent_goal']}。")
        return bool(state.get('result'))
