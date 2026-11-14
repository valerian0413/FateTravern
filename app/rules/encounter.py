"""Deterministic action order and effects for one encounter."""
from __future__ import annotations

from app.rules.combat import absorptions, damage_effects
from app.rules.four_dice import FourDice

ROUND_SECONDS = 8
GOAL_PROGRESS = 3
CONTESTED = {'attack', 'advance', 'withdraw'}


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
        if action['use_preparation'] and actor.get('prepared'):
            preparation = actor.pop('prepared')
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
        self.checks.append({**result, 'actor': actor['name'], 'target': target['name'] if target else None,
                            'purpose': action['kind']})
        return result

    def injury(self, target_id, damage, injury, selection):
        target = self.state['units'][target_id]
        if selection is None:
            target['status'] = 'out'
        if self.fate:
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

    def perform(self, actor_id, action):
        state = self.state
        unit = state['units'][actor_id]
        kind = action['kind']
        is_player = actor_id == self.player_id
        if unit['status'] != 'active':
            return
        unit.pop('guard', None)
        if kind != 'continue' and unit.pop('pending', None):
            self.tell(f"{unit['name']}中断了原来的准备。")
        if kind in ('prepare', 'continue'):
            pending = unit.get('pending')
            if kind == 'prepare':
                pending = unit['pending'] = {'action': action, 'remaining': action['rounds']}
            if not pending:
                self.tell(f"{unit['name']}没有需要继续的准备。")
                return
            pending['remaining'] -= 1
            detail = pending['action']['description']
            if pending['remaining']:
                self.tell(f"{unit['name']}开始／继续{detail}，还需 {pending['remaining']} 轮。")
            else:
                unit.pop('pending')
                if self.roll(actor_id, pending['action'])['success']:
                    unit['prepared'] = detail
                    self.tell(f"{unit['name']}完成{detail}，运用这项准备时可获得一次情境加值。")
                else:
                    self.tell(f"{unit['name']}未能完成{detail}。")
            return

        target_id = action['target_id']
        target = state['units'].get(target_id)
        if kind == 'guard':
            unit['guard'] = True
            self.tell(f"{unit['name']}专注防守，直到下次行动前防守 +1。")
        elif kind == 'wait':
            self.tell(f"{unit['name']}选择等待：{action['description']}。")
        elif kind == 'reposition':
            if is_player:
                for uid in state['order'][1:]:
                    state['units'][uid]['distance'] = 'far'
            else:
                unit['distance'] = 'far'
            self.tell(f"{unit['name']}拉开了距离。")
        elif kind == 'approach' and target and target['status'] == 'active':
            (target if is_player else unit)['distance'] = 'close'
            self.tell(f"{unit['name']}接近了{target['name']}。")
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
            if kind == 'attack' and distance == 'far' and action['skill'] != '射击':
                self.tell(f"{unit['name']}与{target['name']}距离过远，近攻未能出手。")
                return
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
                    state['hit'] = {'target_id': target_id, 'damage': damage, 'injury': action['injury'],
                                    'choices': choices[:3]}
                    return
                self.injury(target_id, damage, action['injury'], choices[0] if choices else None)
            elif kind == 'withdraw' and is_player:
                unit['status'] = 'withdrawn'
                self.tell(f"{unit['name']}成功脱离追逐。")
            else:
                side = 'player' if is_player else 'opponents'
                state['progress'][side] = min(GOAL_PROGRESS, state['progress'][side] + min(damage, 2))
                self.tell(f"{unit['name']}取得先机，己方进度达到 {state['progress'][side]}/{GOAL_PROGRESS}。")
        elif kind in CONTESTED | {'approach'}:
            self.tell(f"{unit['name']}的目标已经离场，这次行动没有执行。")

    def finish(self):
        state = self.state
        player = state['units'][self.player_id]
        if player['status'] != 'active':
            state['result'] = 'player_out' if player['status'] == 'out' else 'player_withdrew'
            self.tell('你已离开当前遭遇。')
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
