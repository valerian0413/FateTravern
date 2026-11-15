"""Register temporary IDs, validate, and apply change ops to a snapshot copy."""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from dataclasses import replace

from app.domain.stress import CAPACITIES, stress_boxes
from app.domain.errors import ValidationError
from app.domain.inventory import currency_names, currency_stack, item_stack
from app.domain.types import (
    CHANGE_OPS,
    CandidateBatch,
    Entity,
    EntityKind,
    Fact,
    FactStatus,
    Issue,
    IssueSeverity,
    Knowledge,
    KnowledgeBasis,
    Relation,
    ScheduledEvent,
    Snapshot,
)


def hash_inputs(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _rewrite(obj, mapping: dict[str, str]):
    if isinstance(obj, str):
        return mapping.get(obj, obj)
    if isinstance(obj, list):
        return [_rewrite(v, mapping) for v in obj]
    if isinstance(obj, dict):
        return {k: _rewrite(v, mapping) for k, v in obj.items()}
    return obj


def register(batch: CandidateBatch | dict, snapshot: Snapshot) -> dict:
    """Allocate UUIDs for new:* refs and deeply rewrite the batch."""
    data = batch if isinstance(batch, dict) else {
        'temp_entities': batch.temp_entities,
        'facts': batch.facts,
        'npc_intents': batch.npc_intents,
        'challenges': batch.challenges,
        'changes': batch.changes,
        'unresolved_refs': batch.unresolved_refs,
    }
    mapping: dict[str, str] = {}
    for index, entity in enumerate(data.get('temp_entities', [])):
        tid = entity.get('id') or entity.get('temp_id')
        if not isinstance(tid, str) or not tid.startswith('new:'):
            raise ValidationError('新实体必须使用唯一 new: 临时ID', path=f'temp_entities[{index}].id')
        if tid in mapping:
            raise ValidationError('临时ID重复', path=f'temp_entities[{index}].id')
        mapping[tid] = str(uuid.uuid4())
    for index, fact in enumerate(data.get('facts', [])):
        tid = fact.get('id') or fact.get('temp_id')
        if isinstance(tid, str) and tid.startswith('new:') and tid not in mapping:
            mapping[tid] = str(uuid.uuid4())
    registered = _rewrite(copy.deepcopy(data), mapping)
    registered['id_map'] = mapping
    return registered


def stabilize_changes(changes: list[dict], world_id: str, turn_id: str, *,
                      existing_fact_ids=()) -> list[dict]:
    """Assign storage IDs once, before preview; repeat calls and retries keep the same IDs."""
    result = copy.deepcopy(changes)
    for index, change in enumerate(result):
        if not isinstance(change, dict):
            continue
        field = 'new_id' if change.get('op') == 'supersede_fact' else 'id'
        if change.get('op') not in {'append_fact', 'supersede_fact', 'learn', 'relate', 'schedule'}:
            continue
        if not change.get(field):
            seed = f"{world_id}:{turn_id}:{change.get('op_id', index)}:{change.get('op')}"
            change[field] = str(uuid.uuid5(uuid.NAMESPACE_URL, seed))
    # Models can refer to an append_fact by its operation ID within the same branch.
    # Explicit fact IDs take precedence, including IDs already persisted in the world.
    fact_ids = set(existing_fact_ids)
    aliases = {}
    for change in result:
        if isinstance(change, dict) and change.get('op') == 'append_fact':
            fact_ids.add(change['id'])
            alias = change.get('op_id')
            if isinstance(alias, str) and alias:
                aliases[alias] = change['id']
    for change in result:
        if isinstance(change, dict) and change.get('op') == 'learn':
            ref = change.get('fact_id')
            if isinstance(ref, str) and ref not in fact_ids and ref in aliases:
                change['fact_id'] = aliases[ref]
    return result


def _currency_change(change, snapshot):
    """Resolve denomination and quantity for both current and pre-migration plans."""
    names = currency_names(snapshot.world)
    legacy = change['op'] == 'resource_delta'
    name = names[-1] if legacy else change.get('currency')
    expected = change.get('expected_value' if legacy else 'expected_quantity')
    delta = change.get('delta')
    if name not in names or type(expected) is not int or type(delta) is not int:
        raise ValidationError('需要已知币种、原数量和整数变动量')
    stack = currency_stack(snapshot.world, snapshot.entities, name)
    quantity = stack.attributes['quantity'] if stack else 0
    if expected != quantity or quantity + delta < 0:
        raise ValidationError('货币数量已变化或余额不足')
    return name, stack, quantity + delta


def validate_changes(changes: list[dict], snapshot: Snapshot) -> list[Issue]:
    issues: list[Issue] = []
    known = set(snapshot.entities)
    known.add(snapshot.world.player_id)
    op_ids = set()
    known_facts = set(snapshot.facts) | {c.get('id') for c in changes if isinstance(c, dict) and c.get('op') == 'append_fact'}
    for index, change in enumerate(changes):
        path = f'changes[{index}]'
        if not isinstance(change, dict) or change.get('op') not in CHANGE_OPS:
            issues.append(Issue(IssueSeverity.BLOCKING, path, '非法或缺失 op', '使用已知状态操作'))
            continue
        op_id = change.get('op_id')
        if not isinstance(op_id, str) or not op_id.strip() or op_id in op_ids:
            issues.append(Issue(IssueSeverity.BLOCKING, f'{path}.op_id', 'op_id 缺失、为空或重复', '为每项操作提供唯一稳定 op_id'))
        else:
            op_ids.add(op_id)
        op = change['op']
        if op == 'create_entity':
            kind = change.get('kind')
            if kind not in {k.value for k in EntityKind}:
                issues.append(Issue(IssueSeverity.BLOCKING, f'{path}.kind', '非法实体类型', '使用合法 kind'))
            else:
                known.add(change.get('id', ''))
        elif op in ('set_attribute', 'transfer_item'):
            eid = change.get('entity_id') or change.get('item_id')
            if eid not in known and eid not in snapshot.entities:
                issues.append(Issue(IssueSeverity.BLOCKING, path, f'未知实体 {eid}', '先注册实体'))
        elif op == 'currency_delta' or (op == 'resource_delta' and change.get('key') == 'coins'):
            try:
                _currency_change(change, snapshot)
            except ValidationError as exc:
                issues.append(Issue(IssueSeverity.BLOCKING, path, exc.message, '核对币种和数量'))
        elif op == 'resource_delta':
            resources = snapshot.entities[snapshot.world.player_id].attributes.get('resources', {})
            key = change.get('key')
            expected = change.get('expected_value')
            delta = change.get('delta')
            if key not in resources or resources[key] != expected:
                issues.append(Issue(IssueSeverity.BLOCKING, path, '资源前置条件不满足', '核对 expected_value'))
            elif type(delta) is not int or resources[key] + delta < 0:
                issues.append(Issue(IssueSeverity.BLOCKING, path, '资源不足或 delta 非法', '调整 delta'))
        elif op == 'append_fact':
            subject = change.get('subject_id')
            if subject not in known and subject not in snapshot.entities:
                issues.append(Issue(IssueSeverity.BLOCKING, f'{path}.subject_id', '未知主体', '引用已有实体'))
        elif op == 'supersede_fact':
            if change.get('fact_id') not in snapshot.facts:
                issues.append(Issue(IssueSeverity.BLOCKING, f'{path}.fact_id', '事实不存在', '使用有效事实ID'))
        elif op == 'learn':
            if change.get('fact_id') is not None and change['fact_id'] not in known_facts:
                issues.append(Issue(IssueSeverity.BLOCKING, f'{path}.fact_id', '认知引用的事实不存在', '引用已有或本轮新增事实'))
            if change.get('observer_id') not in known and change.get('observer_id') not in snapshot.entities:
                issues.append(Issue(IssueSeverity.BLOCKING, f'{path}.observer_id', '未知观察者', '引用合法实体'))
        elif op == 'relate':
            for key in ('source_entity_id', 'target_entity_id'):
                if change.get(key) not in known and change.get(key) not in snapshot.entities:
                    issues.append(Issue(IssueSeverity.BLOCKING, f'{path}.{key}', '未知实体', '引用合法实体'))
        elif op == 'schedule':
            due = change.get('due_at')
            if type(due) is not int or due < snapshot.world.clock:
                issues.append(Issue(IssueSeverity.BLOCKING, f'{path}.due_at', '非法计划时间', 'due_at >= clock'))
        elif op == 'finish_schedule':
            event = snapshot.scheduled.get(change.get('id', ''))
            if not event or event.status != 'pending' or event.due_at > snapshot.world.clock:
                issues.append(Issue(IssueSeverity.BLOCKING, path, '事件未到期或已处理', '检查计划状态'))
    return issues


def apply_to_copy(snapshot: Snapshot, changes: list[dict], *, source_event_id: str | None = None) -> Snapshot:
    issues = validate_changes(changes, snapshot)
    blocking = [i for i in issues if i.severity == IssueSeverity.BLOCKING]
    if blocking:
        raise ValidationError(blocking[0].reason, path=blocking[0].path)

    state = Snapshot(
        world=replace(snapshot.world),
        entities=copy.deepcopy(snapshot.entities),
        facts=copy.deepcopy(snapshot.facts),
        knowledge=copy.deepcopy(snapshot.knowledge),
        relations=copy.deepcopy(snapshot.relations),
        npc_cards=copy.deepcopy(snapshot.npc_cards),
        scheduled=copy.deepcopy(snapshot.scheduled),
    )
    player = state.entities[state.world.player_id]

    for change in changes:
        op = change['op']
        if op == 'create_entity':
            eid = change['id']
            if not eid or eid in state.entities:
                raise ValidationError('实体ID无效或已存在', path='create_entity.id')
            # Cross-world refs are prevented by repositories; here reject foreign world_id.
            if change.get('world_id') and change['world_id'] != state.world.id:
                raise ValidationError('禁止跨世界引用', path='create_entity.world_id')
            attributes = dict(change.get('attributes') or {})
            if change['kind'] == EntityKind.ITEM.value:
                quantity = attributes.get('quantity', 1)
                if 'currency' in attributes or type(quantity) is not int or quantity < 1:
                    raise ValidationError('普通物品必须有正整数数量；货币使用 currency_delta')
            state.entities[eid] = Entity(
                id=eid,
                world_id=state.world.id,
                kind=EntityKind(change['kind']),
                name=change['name'],
                attributes=attributes,
                revision=1,
                source_event_id=source_event_id,
            )
        elif op in ('taken_out', 'concede', 'encounter_concede'):
            entity = state.entities.get(change.get('entity_id'))
            if state.world.ruleset_id != 'fate-lite-v1' or not entity:
                raise ValidationError('出局或退让需要有效的战斗角色')
            if op != 'encounter_concede':
                entity.attributes['combat_status'] = op
            if op in ('concede', 'encounter_concede'):
                scars = entity.attributes.get('consequences', {})
                slots = change.get('scar_slots', scars) if op == 'encounter_concede' else scars
                count = sum(bool(scar.get('text')) and scar.get('this_scene', False)
                            for slot, scar in scars.items() if slot in slots)
                entity.attributes['fate_points'] = entity.attributes.get('fate_points', 0) + 1 + count
            entity.revision += 1
        elif op in ('mark_stress', 'clear_stress', 'take_consequence'):
            entity = state.entities.get(change.get('entity_id'))
            if state.world.ruleset_id != 'fate-lite-v1' or not entity:
                raise ValidationError('压力与后果操作不适用于该角色或规则')
            track = stress_boxes(entity.attributes)
            if track is None and entity.kind == EntityKind.NPC:
                track = [False] * 3
                entity.attributes.setdefault('consequences', {
                    slot: {'capacity': capacity, 'text': None}
                    for slot, capacity in CAPACITIES.items()
                })
            if track is None:
                raise ValidationError('此角色没有压力轨')
            entity.attributes['stress_tracks'] = {'unified': track}
            if op == 'mark_stress':
                boxes = change.get('boxes')
                if change.get('track', 'unified') != 'unified' or not isinstance(boxes, list) or len(boxes) != 1 or type(boxes[0]) is not int:
                    raise ValidationError('一次受击最多使用一个压力格，请指定单一格位（索引 0—2）')
                if len(set(boxes)) != len(boxes) or any(i<0 or i>=len(track) or track[i] for i in boxes):
                    raise ValidationError('压力格已使用或越界')
                for i in boxes: track[i] = True
            elif op == 'clear_stress':
                if not isinstance(change.get('reason'),str) or not change['reason'].strip():
                    raise ValidationError('清除压力需要场景结束依据')
                track[:] = [False] * 3
                entity.attributes.pop('combat_status', None)
                scars = entity.attributes.get('consequences', {})
                for scar in scars.values():
                    scar.pop('this_scene', None)
                mild = scars.get('mild')
                if mild:
                    mild['text'] = None
            else:
                slot = entity.attributes.get('consequences', {}).get(change.get('slot'))
                if not slot or slot.get('text') is not None or not isinstance(change.get('text'),str) or not change['text'].strip():
                    raise ValidationError('后果槽不可用或缺少创伤描述')
                slot['text'] = change['text']
                slot['this_scene'] = True
            entity.revision += 1
        elif op == 'update_npc_card':
            from app.modules.npc import build_card, validate_update
            entity = state.entities.get(change.get('entity_id'))
            if entity is None or entity.kind != EntityKind.NPC:
                raise ValidationError('角色卡只能更新当前世界 NPC', path='npc_card.entity_id')
            card = state.npc_cards.get(entity.id) or build_card({'id': entity.id, 'name': entity.name, 'attributes': entity.attributes})
            validate_update(change, card, state.entities)
            state.npc_cards[entity.id] = replace(card, **change['patch'])
        elif op == 'set_attribute':
            entity = state.entities[change['entity_id']]
            if entity.revision != change.get('expected_revision', entity.revision):
                raise ValidationError('属性修订前置条件不满足', path='set_attribute.expected_revision')
            key = change['key']
            if key == 'currency' or (entity.attributes.get('currency') and key in ('quantity', 'owner')):
                raise ValidationError('货币堆叠须使用按币种变更操作', path='set_attribute.key')
            if entity.kind == EntityKind.ITEM and key == 'quantity':
                if type(change.get('value')) is not int or change['value'] < 0:
                    raise ValidationError('物品数量须为非负整数', path='set_attribute.value')
            if state.world.ruleset_id == 'fate-lite-v1' and entity.kind == EntityKind.PLAYER and key in {'skills','aspects','stress_tracks','consequences','stunts','ruleset_id','character_template','resources','hp','stress','fate_points','combat_status'}:
                raise ValidationError('此角色数据不能通过通用属性操作修改', path='set_attribute.key')
            if entity.attributes.get(key) != change.get('expected'):
                raise ValidationError('属性前置条件不满足', path='set_attribute.expected')
            entity.attributes[key] = change['value']
            entity.revision += 1
        elif op == 'currency_delta' or (op == 'resource_delta' and change.get('key') == 'coins'):
            name, stack, quantity = _currency_change(change, state)
            if stack:
                stack.attributes['quantity'] = quantity
                stack.revision += 1
            elif quantity:
                stack = item_stack(state.world, name, quantity, currency=True)
                stack.source_event_id = source_event_id
                state.entities[stack.id] = stack
        elif op == 'resource_delta':
            resources = player.attributes.setdefault('resources', {})
            key, delta = change['key'], change['delta']
            if resources.get(key) != change['expected_value']:
                raise ValidationError('资源前置条件不满足', path='resource_delta')
            if resources[key] + delta < 0:
                raise ValidationError('资源不足', path='resource_delta')
            resources[key] += delta
        elif op == 'transfer_item':
            item = state.entities[change['item_id']]
            if item.kind != EntityKind.ITEM:
                raise ValidationError('不是物品', path='transfer_item')
            if item.attributes.get('currency'):
                raise ValidationError('收付货币须说明币种和数量', path='transfer_item')
            if item.attributes.get('owner') != change.get('expected_owner'):
                raise ValidationError('物品归属前置条件不满足', path='transfer_item')
            item.attributes['owner'] = change['to']
            item.revision += 1
        elif op == 'append_fact':
            fid = change.get('id') or str(uuid.uuid4())
            state.facts[fid] = Fact(
                id=fid,
                world_id=state.world.id,
                subject_id=change['subject_id'],
                predicate=change['predicate'],
                value=change['value'],
                status=FactStatus.ACTIVE,
                source_event_id=source_event_id,
                disclosure_rule=change.get('disclosure_rule'),
            )
        elif op == 'supersede_fact':
            old = state.facts[change['fact_id']]
            old.status = FactStatus.SUPERSEDED
            new_id = change.get('new_id') or str(uuid.uuid4())
            state.facts[new_id] = Fact(
                id=new_id,
                world_id=state.world.id,
                subject_id=change.get('subject_id', old.subject_id),
                predicate=change.get('predicate', old.predicate),
                value=change['value'],
                status=FactStatus.ACTIVE,
                source_event_id=source_event_id,
                supersedes_id=old.id,
                disclosure_rule=change.get('disclosure_rule', old.disclosure_rule),
            )
        elif op == 'learn':
            kid = change.get('id') or str(uuid.uuid4())
            state.knowledge[kid] = Knowledge(
                id=kid,
                world_id=state.world.id,
                observer_id=change['observer_id'],
                basis=KnowledgeBasis(change.get('basis', 'observed')),
                source_event_id=source_event_id or change.get('source_event_id') or '',
                learned_at=state.world.clock,
                fact_id=change.get('fact_id'),
                belief_text=change.get('belief_text'),
            )
        elif op == 'relate':
            rid = change.get('id') or str(uuid.uuid4())
            state.relations[rid] = Relation(
                id=rid,
                source_entity_id=change['source_entity_id'],
                target_entity_id=change['target_entity_id'],
                kind=change.get('kind', 'generic'),
                description=change['description'],
                evidence_event_ids=list(change.get('evidence_event_ids') or []),
            )
        elif op == 'schedule':
            sid = change.get('id') or str(uuid.uuid4())
            state.scheduled[sid] = ScheduledEvent(
                id=sid,
                world_id=state.world.id,
                due_at=change['due_at'],
                prerequisites=list(change.get('prerequisites') or []),
                actor_ids=list(change.get('actor_ids') or []),
                intent=change['intent'],
                status='pending',
                source_event_id=source_event_id,
            )
        elif op == 'finish_schedule':
            event = state.scheduled[change['id']]
            event.status = 'finished'
        else:
            raise ValidationError(f'不支持的状态操作 {op}', path='op')
    return state
