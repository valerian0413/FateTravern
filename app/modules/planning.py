"""Host action planning, independent of entity generation and dice execution."""
import json
import uuid
from copy import deepcopy

from app.domain.changes import apply_to_copy, stabilize_changes
from app.domain.errors import ValidationError
from app.prompts.loader import load
from app.providers.base import ModelRequest
from app.rules.four_dice import FourDice

DIFFICULTY_LABELS = {'easy': '简单', 'standard': '普通', 'hard': '困难', 'extreme': '极难'}


def validate_options(options, required=False):
    if not isinstance(options, list) or (options and not 2 <= len(options) <= 4) or (required and not options):
        raise ValidationError('有选择时必须提供 2—4 个建议', path='options')
    if any(not isinstance(x, str) or not x.strip() or len(x) > 160 for x in options):
        raise ValidationError('建议必须是简短行动文本', path='options')
    if len(set(options)) != len(options):
        raise ValidationError('行动建议不能重复', path='options')
    return options


def validate_plan(data, context):
    if not isinstance(data, dict) or not isinstance(data.get('challenge'), dict):
        raise ValidationError('行动规划缺少 challenge，不能默认成功', path='plan.challenge')
    plan = deepcopy(data)
    c = plan['challenge']
    if type(c.get('duration_minutes')) is not int or not 0 <= c['duration_minutes'] <= 1440:
        raise ValidationError('行动时长必须为 0—1440 分钟', path='challenge.duration_minutes')
    if c.get('mode') in ('check', 'opposed') and (not isinstance(c.get('domain'), str) or not c['domain'].strip()):
        raise ValidationError('检定需要行动领域', path='challenge.domain')
    FourDice(context.get('rules')).assess(c, {'skills': context.get('skills', {}), 'aspects': context.get('aspects', [])})
    if c.get('mode') == 'opposed':
        battle = c.get('combat')
        fields = ('attacker_id', 'defender_id', 'defense_domain', 'injury')
        if (not isinstance(battle, dict) or any(not isinstance(battle.get(field), str)
                or not battle[field].strip() for field in fields)
                or battle['attacker_id'] == battle['defender_id'] or len(battle['injury']) > 100):
            raise ValidationError('对抗检定需要不同的攻防角色、防守技能和伤痕描述', path='challenge.combat')
        if context.get('rules', {}).get('ruleset_id') != 'fate-lite-v1':
            raise ValidationError('对抗检定适用于 Fate 规则', path='challenge.mode')
    timing = plan.get('choice_timing', 'before_action' if plan.get('needs_choice') else 'none')
    if timing not in ('none','before_action','after_action'):
        raise ValidationError('非法暂停时机', path='choice_timing')
    if 'choice_timing' in plan and timing=='none' and plan.get('needs_choice'):
        raise ValidationError('需要选择时必须说明行动前或行动后暂停', path='choice_timing')
    plan['choice_timing'] = timing
    if timing != 'none': plan['needs_choice'] = True
    if type(plan.get('needs_choice')) is not bool:
        raise ValidationError('needs_choice 必须为布尔值', path='plan')
    plan['options'] = validate_options(plan.get('options', []), plan['needs_choice'])
    if c['mode'] == 'blocked':
        plan['needs_choice'] = True
        plan['choice_timing'] = 'before_action'
        validate_options(plan['options'], required=True)
    for field in ('prompt', 'public_risk'):
        if not isinstance(plan.get(field), str) or len(plan[field]) > 400:
            raise ValidationError('缺少或过长的公开说明', path=field)
    if plan['needs_choice'] and (len(plan['prompt'])>120 or any(q in plan['prompt'] for q in ('『','』','“','”'))):
        raise ValidationError('选择提示只能是简短场外问题，NPC 台词须进入本轮后果，使用 after_action', path='prompt')
    if plan['needs_choice'] and not plan['prompt'].strip():
        raise ValidationError('暂停需要说明新的选择或危险', path='prompt')
    if c['mode'] == 'check' and not plan['public_risk'].strip():
        raise ValidationError('检定必须说明可预见风险', path='public_risk')
    for key in ('success_events', 'failure_events'):
        values = plan.get(key, [])
        if not isinstance(values, list) or any(not isinstance(v, str) or len(v) > 400 for v in values):
            raise ValidationError('公开后果必须是简短文本数组', path=key)
        plan[key] = values
    return plan


def validate_effects(plan, snapshot, allowed_npc_ids, *, turn_id):
    challenge = plan['challenge']
    if challenge['mode'] == 'opposed':
        combat = challenge['combat']
        if snapshot.world.player_id not in (combat['attacker_id'], combat['defender_id']):
            raise ValidationError('对抗须有玩家参与', path='challenge.combat')
        for field in ('attacker_id', 'defender_id'):
            entity = snapshot.entities.get(combat[field])
            if entity is None or entity.kind.value not in ('player', 'npc') or entity.attributes.get('combat_status'):
                raise ValidationError('攻防方必须是尚在场的角色', path=f'challenge.combat.{field}')
        defense_bonus = snapshot.entities[combat['defender_id']].attributes.get('skills', {}).get(combat['defense_domain'], 0)
        if type(defense_bonus) is not int:
            raise ValidationError('防守加值必须是整数', path='challenge.combat.defense_domain')
    for branch in ('success_effects', 'failure_effects'):
        effects = plan['challenge'].get(branch, [])
        if not isinstance(effects, list):
            raise ValidationError('后果必须是操作数组', path=branch)
        for effect in effects:
            if not isinstance(effect, dict):
                raise ValidationError('后果必须是操作对象', path=branch)
            if effect.get('op') in ('taken_out', 'concede', 'encounter_concede') or (challenge['mode'] == 'opposed'
                    and effect.get('op') in ('mark_stress', 'take_consequence', 'clear_stress')):
                raise ValidationError('对抗伤害和退让由规则引擎结算', path=branch)
            if effect.get('op') == 'update_npc_card' and effect.get('entity_id') not in allowed_npc_ids:
                raise ValidationError('不能修改无关 NPC 的角色卡', path='npc_card.entity_id')
            if effect.get('op') == 'set_attribute' and effect.get('entity_id') == snapshot.world.player_id:
                if effect.get('key') in {'resources', 'skills', 'hp', 'stress', 'coins'}:
                    raise ValidationError('不能通过普通属性操作改写角色能力或资源；资源使用 resource_delta', path='set_attribute.key')
        try:
            effects = stabilize_changes(effects, snapshot.world.id, turn_id, existing_fact_ids=snapshot.facts)
            apply_to_copy(snapshot, effects, source_event_id='plan-validation')
            plan['challenge'][branch] = effects
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError('状态操作字段无效：' + str(exc), path=branch) from None
    return plan


async def propose(provider, settings, context, fixture=None, snapshot=None, allowed_npc_ids=()):
    def checked(data):
        if provider is not None and fixture is None and isinstance(data,dict) and 'choice_timing' not in data:
            raise ValidationError('缺少 choice_timing，请明确 none/before_action/after_action', path='choice_timing')
        plan = validate_plan(data, context)
        return validate_effects(plan, snapshot, allowed_npc_ids, turn_id=context['turn_id']) if snapshot is not None else plan
    if fixture is not None:
        return checked(fixture)
    if provider is None:
        # Offline fixtures only. Production requires an explicit model plan.
        c = context.get('legacy_challenges') or [{
            'mode': 'automatic', 'goal': context['action'], 'duration_minutes': 1,
            'success_effects': [], 'failure_effects': [],
        }]
        return checked({'challenge': c[0], 'needs_choice': False, 'prompt': '',
                        'options': [], 'public_risk': '可能未能达成目标'})
    spec = load('planning', settings.prompt_version, settings.prompts_dir)
    messages = [{'role': 'system', 'content': spec.text},
                {'role': 'user', 'content': json.dumps(context, ensure_ascii=False)}]
    for attempt in range(2):
        response = await provider.complete(ModelRequest(
            module='planning', prompt_version=settings.prompt_version, schema_version='2',
            world_id=context['world_id'], turn_id=context['turn_id'], call_id=uuid.uuid4().hex,
            output_budget=3000, messages=messages,
        ))
        try:
            return checked(response.content)
        except ValidationError as exc:
            if attempt:
                raise
            messages = messages + [
                {'role': 'assistant', 'content': json.dumps(response.content, ensure_ascii=False)},
                {'role': 'user', 'content': json.dumps({'repair': exc.message, 'path': exc.path,
                  'instruction': '修正整个方案，核对输入的真实字段。不要为无关动作添加数值收益，不要修改原始世界状态。'}, ensure_ascii=False)}]


def check_notice(plan):
    c = plan['challenge']
    if c['mode'] != 'check':
        return ''
    return f"检定：{c['domain']} · {DIFFICULTY_LABELS[c['difficulty']]}。可预见风险：{plan['public_risk']}"
