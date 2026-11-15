"""Select the skills and practical means for a planned encounter action."""
from __future__ import annotations

import json
import uuid

from app.domain.errors import ModelRequestError, ModelUnavailable, ValidationError
from app.prompts.loader import load
from app.providers.base import ModelRequest, call_model
from app.rules.character import SKILL_USES, SKILLS
from app.rules.encounter import CONTESTED, OBJECTIVE
from app.modules.encounter import planned_actions


TOOL_NAME = 'submit_encounter_checks'
CLARIFY_NAME = 'clarify_encounter_skill'
CHECK_FIELDS = {
    'skill': {'type': 'string', 'enum': SKILLS},
    'defense_skill': {'type': 'string', 'enum': SKILLS},
    'reach': {'type': 'string', 'enum': ['close', 'far']},
    'equipment': {'type': 'string'},
    'reason': {'type': 'string'},
}
CLARIFY_TOOL = {'type': 'function', 'function': {
    'name': CLARIFY_NAME,
    'description': '玩家的方法或装备缺少决定检定的关键信息时，问一句具体的问题。',
    'parameters': {
        'type': 'object', 'additionalProperties': False, 'required': ['question'],
        'properties': {'question': {'type': 'string'}},
    },
}}


def required_checks(plan, encounter):
    checks = {}
    for key, uid, action in planned_actions(plan):
        kind = action['kind']
        if kind in ('search', 'study', 'prepare'):
            spec = {'mode': 'check', 'requires_reach': False}
        elif kind in CONTESTED and (kind != 'withdraw' or encounter['kind'] == 'chase'):
            target = encounter['player_id'] if action['target_id'] == OBJECTIVE else action['target_id']
            spec = {
                'mode': 'opposed', 'defender_id': target,
                'requires_reach': kind == 'attack' or action['target_id'] == OBJECTIVE,
            }
        else:
            continue
        checks[f'check_{len(checks) + 1}'] = {'actor_id': uid, 'action_key': key, **spec}
    return checks


def check_tool(required):
    entries = {}
    for check_id, spec in required.items():
        mandatory = ['skill', 'reason']
        if spec['mode'] == 'opposed':
            mandatory.append('defense_skill')
        if spec['requires_reach']:
            mandatory.append('reach')
        entries[check_id] = {
            'type': 'object',
            'properties': {field: CHECK_FIELDS[field] for field in [*mandatory, 'equipment']},
            'required': mandatory, 'additionalProperties': False,
        }
    return {'type': 'function', 'function': {
        'name': TOOL_NAME,
        'description': '按给定检定 ID 提交整轮的技能选择。每项攻防对抗在同一项填写攻防技能。',
        'parameters': {
            'type': 'object', 'additionalProperties': False, 'required': ['checks'],
            'properties': {'checks': {
                'type': 'object', 'properties': entries, 'required': list(entries), 'additionalProperties': False,
            }},
        },
    }}


def read_call(message, allowed):
    calls = message.get('tool_calls')
    if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
        raise ValidationError('请从可用工具中单独调用一个')
    call = calls[0]
    function = call.get('function')
    if (call.get('type') != 'function' or not isinstance(function, dict) or function.get('name') not in allowed
            or not isinstance(call.get('id'), str) or not call['id']):
        raise ValidationError('技能 Agent 的工具调用无效')
    try:
        result = json.loads(function.get('arguments'))
    except (ValueError, TypeError):
        raise ValidationError('技能工具参数须为有效 JSON') from None
    if not isinstance(result, dict):
        raise ValidationError('技能工具参数须为对象')
    return call['id'], function['name'], result


def validate_checks(rows, required, payload):
    if not isinstance(rows, dict) or rows.keys() != required.keys():
        raise ValidationError(
            f"checks 必须是对象，且键恰好为 {'、'.join(required)}。"
            '只填写给出的检定。对抗双方的技能填在同一项，防守使用 defense_skill。'
        )
    resolved = {}
    player = payload['encounter']['player_id']
    available = set(payload['inventory']) | set(payload['scene_items'])
    for check_id, spec in required.items():
        row = rows[check_id]
        if not isinstance(row, dict) or row.keys() - CHECK_FIELDS.keys():
            raise ValidationError('技能判定包含无效字段')
        uid = spec['actor_id']
        skill, defense, reach = row.get('skill'), row.get('defense_skill'), row.get('reach')
        reason, equipment = row.get('reason'), row.get('equipment', '')
        if skill not in SKILLS or (spec['mode'] == 'opposed' and defense not in SKILLS):
            raise ValidationError('须从技能目录选择技能，对抗检定还须明确防守技能')
        if spec['mode'] == 'check' and defense is not None:
            raise ValidationError('单方检定没有防守技能')
        if (spec['requires_reach'] and reach not in ('close', 'far')
                or not spec['requires_reach'] and reach is not None):
            raise ValidationError('只有需校验距离的动作才填写有效射程')
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 100:
            raise ValidationError('请用 1 至 100 字说明技能选择依据')
        if not isinstance(equipment, str) or (uid == player and equipment and equipment not in available):
            raise ValidationError('玩家装备必须使用物品栏或场景中已知的完整名称')
        if uid == player and reach and skill == '射击' and (reach != 'far' or not equipment):
            raise ValidationError('射击须填写远程射程和可用的远程装备')
        resolved[spec['action_key']] = {'skill': skill, 'defense_skill': defense, 'reach': reach,
                         'equipment': equipment, 'skill_reason': reason.strip()}
    return resolved


async def select(runtime, turn, payload, plan):
    required = required_checks(plan, payload['encounter'])
    if not required:
        return {}, ''
    names = {participant['id']: participant['name'] for participant in payload['participants']}
    can_clarify = any(spec['actor_id'] == payload['encounter']['player_id'] for spec in required.values())
    context = {key: value for key, value in payload.items() if key not in ('action', 'order', 'player_text')}
    if can_clarify:
        context['player_text'] = payload['player_text']
    context['skill_catalog'] = SKILL_USES
    actions = {key: action for key, uid, action in planned_actions(plan)}
    context['pending_checks'] = {
        check_id: {
            **spec, 'actor_name': names[spec['actor_id']], 'defender_name': names.get(spec.get('defender_id')),
            'kind': actions[spec['action_key']]['kind'], 'description': actions[spec['action_key']]['description'],
            'requires_previous_success': actions[spec['action_key']].get('requires_previous_success', False),
        }
        for check_id, spec in required.items()
    }
    if (fixture := (runtime.fixtures or {}).get('encounter_skills')) is not None:
        return validate_checks(fixture, required, payload), ''
    if runtime.provider is None:
        raise ModelUnavailable('技能 Agent 不可用，请恢复模型连接后重试')
    settings = runtime.settings
    spec = load('encounter_skills', settings.prompt_version, settings.prompts_dir)
    tools = [check_tool(required)]
    if can_clarify:
        tools.append(CLARIFY_TOOL)
    allowed = [tool['function']['name'] for tool in tools]
    for attempt in range(2):
        response = await call_model(runtime.provider, ModelRequest(
            module=spec.module, prompt_version=spec.version, schema_version='2',
            world_id=turn.world_id, turn_id=turn.id, call_id=uuid.uuid4().hex,
            output_budget=1200, output_mode='text', tools=tools, thinking='disabled',
            messages=[
                {'role': 'system', 'content': spec.text},
                {'role': 'user', 'content': json.dumps(context, ensure_ascii=False)},
            ],
        ))
        try:
            call_id, name, args = read_call(response.message, allowed)
            if name == CLARIFY_NAME:
                question = args.get('question')
                if set(args) != {'question'} or not isinstance(question, str) or not question.strip() or len(question) > 120:
                    raise ValidationError('请提交一个简短的澄清问题')
                resolved, question = {}, question.strip()
            else:
                if set(args) != {'checks'}:
                    raise ValidationError('技能工具需要以检定 ID 为键的 checks 对象')
                resolved, question = validate_checks(args['checks'], required, payload), ''
        except ValidationError as exc:
            if attempt:
                raise ModelRequestError('技能 Agent 未能提交有效检定：' + exc.message,
                                        code='encounter_skill_call_invalid', retryable=True) from exc
            context['correction'] = exc.message
            continue
        runtime.trace.write({'event': 'agent.tool', 'world_id': turn.world_id, 'turn_id': turn.id,
                             'name': name, 'tool_call_id': call_id, 'status': 'selected'})
        return resolved, question
