"""Native model tools for selecting a narrative path and reading public context."""
from __future__ import annotations

import json
import uuid

from app.domain.errors import ModelRequestError
from app.domain.types import Intent, IntentKind
from app.prompts.loader import load
from app.providers.base import ModelRequest
from app.application.memory_search import search_memory


def _function(name, description, required, **fields):
    properties = {key: {'type': kind} for key, kind in fields.items()}
    for field in properties.values():
        if field['type'] == 'array':
            field['items'] = {'type': 'string'}
    return {'type': 'function', 'function': {
        'name': name, 'description': description,
        'parameters': {'type': 'object', 'properties': properties,
                       'required': required, 'additionalProperties': False},
    }}


LOOKUP = 'lookup_public'
TOOLS = [
    _function(LOOKUP, '按名称或自然语言查找玩家可见的人物、地点、设定和历史。', ['query'], query='string'),
    _function('reply_to_host', '把咨询回复写入与主持人交谈。', ['reply'], reply='string', suggested_action='string'),
    _function('resolve_for_narrative', '提交玩家授权的行动。游戏程序结算后写入旅途见闻。',
              ['action'], action='string', target_ids='array', world_gaps='array'),
    _function('start_encounter', '启动需要多个单位轮流行动的战斗、追逐或限时谈判。',
              ['kind', 'action'], kind='string', action='string', target_ids='array'),
    _function('play_encounter_turn', '已有遭遇时，提交玩家在当前轮授权的一个动作。',
              ['action'], action='string'),
    _function('propose_world_change', '将玩家原文送入提案审核，结果写入与主持人交谈。', []),
    _function('ask_player', '在与主持人交谈提出一个澄清问题。', ['question'], question='string'),
]
SCHEMAS = {tool['function']['name']: tool['function']['parameters'] for tool in TOOLS}
MAX_MODEL_STEPS = 5


def _arguments(call):
    function = call.get('function')
    if call.get('type') != 'function' or not isinstance(function, dict):
        raise ValueError('需要 function 工具')
    name = function.get('name')
    if not isinstance(name, str) or name not in SCHEMAS:
        raise ValueError('未知工具')
    raw = function.get('arguments')
    if not isinstance(raw, str) or len(raw) > 16000:
        raise ValueError('工具参数必须为 JSON 对象')
    args = json.loads(raw)
    schema = SCHEMAS[name]
    properties = schema['properties']
    if not isinstance(args, dict) or args.keys() - properties.keys() or not all(k in args for k in schema['required']):
        raise ValueError('工具参数缺失或包含未知字段')
    for key, value in args.items():
        if properties[key]['type'] == 'string':
            if not isinstance(value, str) or not value.strip() or len(value) > 4000:
                raise ValueError('工具文本参数无效')
        elif not isinstance(value, list) or len(value) > 8 or any(
                not isinstance(item, str) or not item.strip() or len(item) > 400 for item in value):
            raise ValueError('工具列表参数无效')
    return name, args


def _terminal(name, args, text, public):
    defaults = {'target_ids': [], 'creation_statements': [], 'needs_clarification': False, 'requested_tools': []}
    if name == 'reply_to_host':
        suggested = args.get('suggested_action')
        return Intent(kind=IntentKind.DISCUSSION, **defaults), {
            'reply': args['reply'], 'suggested_action': suggested, 'can_execute': bool(suggested),
        }
    if name == 'ask_player':
        return Intent(kind=IntentKind.CLARIFY, question=args['question'], **{**defaults, 'needs_clarification': True}), None
    if name == 'propose_world_change':
        if public.get('encounter'):
            raise ValueError('当前遭遇中可以咨询或行动，场景结束后才能变更设定')
        return Intent(kind=IntentKind.COCREATE, **{**defaults, 'creation_statements': [text]}), None
    visible = {entity['id'] for entity in public.get('entities', [])}
    if name in ('start_encounter', 'play_encounter_turn'):
        from app.modules.encounter import KINDS
        active = public.get('encounter')
        if name == 'start_encounter' and (active or args['kind'] not in KINDS):
            raise ValueError('已有遭遇时请继续当前遭遇，新遭遇类型须为 combat、chase 或 negotiation')
        if name == 'play_encounter_turn' and not active:
            raise ValueError('当前没有遭遇')
        return Intent(kind=IntentKind.ENCOUNTER, normalized_action=args['action'],
                      encounter_kind=args.get('kind'), **{**defaults, 'target_ids': [
                          item for item in args.get('target_ids', []) if item in visible]}), None
    if public.get('encounter'):
        raise ValueError('当前有遭遇，执行行动请调用 play_encounter_turn')
    return Intent(kind=IntentKind.EXPLORE, normalized_action=args['action'],
                  unresolved=args.get('world_gaps', []),
                  **{**defaults, 'target_ids': [item for item in args.get('target_ids', []) if item in visible]}), None


async def select_host_tool(runtime, context, text, mode_hint):
    """Execute read calls and return one terminal call for the CrewAI branch."""
    spec = load('host_tools', runtime.settings.prompt_version, runtime.settings.prompts_dir)
    world_id = context['world_id']
    public = {key: value for key, value in context['public'].items() if key != 'places'}
    messages = [
        {'role': 'system', 'content': spec.text},
        {'role': 'user', 'content': json.dumps({
            'text': text, 'mode_hint': mode_hint, 'public': public, 'history': context.get('history', []),
        }, ensure_ascii=False)},
    ]
    seen_ids = set()
    lookup_cache = {}
    for step in range(MAX_MODEL_STEPS):
        tools = TOOLS if step < MAX_MODEL_STEPS - 1 else TOOLS[1:]
        response = await runtime.provider.complete(ModelRequest(
            module='host_tools', prompt_version=spec.version, schema_version='1',
            world_id=world_id, turn_id=context['turn_id'], call_id=uuid.uuid4().hex,
            messages=messages, output_budget=1600, output_mode='text', tools=tools,
        ))
        calls = response.message.get('tool_calls')
        if not calls:
            messages.extend([
                response.message,
                {'role': 'user', 'content': '请调用一个工具完成请求。普通文本不会发给玩家。'},
            ])
            continue
        if not isinstance(calls, list) or len(calls) > 4 or any(
                not isinstance(call, dict) or not isinstance(call.get('id'), str)
                or not call['id'] or len(call['id']) > 256 for call in calls):
            raise ModelRequestError('主持人返回了无法关联结果的工具调用', code='host_tool_protocol_invalid')
        ids = {call['id'] for call in calls}
        if len(ids) != len(calls) or ids & seen_ids:
            raise ModelRequestError('主持人返回了重复的工具调用 ID', code='host_tool_protocol_invalid')
        seen_ids.update(ids)
        messages.append(response.message)
        for call in calls:
            name = None
            try:
                name, args = _arguments(call)
                if name == LOOKUP:
                    if step == MAX_MODEL_STEPS - 1:
                        raise ValueError('查询轮次已用尽')
                    query = args['query'].strip()
                    if query not in lookup_cache:
                        lookup_cache[query] = await search_memory(runtime, world_id, query)
                    result = lookup_cache[query]
                elif len(calls) == 1:
                    runtime.trace.write({
                        'event': 'agent.tool', 'turn_id': context['turn_id'], 'world_id': world_id,
                        'tool_call_id': call['id'], 'name': name, 'status': 'selected',
                    })
                    return _terminal(name, args, text, public)
                else:
                    raise ValueError('请单独调用一个输出工具')
            except ValueError as error:
                result = {'error': str(error)}
            runtime.trace.write({
                'event': 'agent.tool', 'turn_id': context['turn_id'], 'world_id': world_id,
                'tool_call_id': call['id'], 'name': name, 'result': result,
                'status': 'rejected' if 'error' in result else 'completed',
            })
            messages.append({'role': 'tool', 'tool_call_id': call['id'],
                             'content': json.dumps(result, ensure_ascii=False)})
    raise ModelRequestError('主持人未能选定输出工具，请重试', code='host_tool_loop_exhausted', retryable=True)
