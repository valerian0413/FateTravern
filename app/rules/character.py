"""Versioned simplified Fate character creation and damage data."""
from copy import deepcopy
from app.domain.errors import ValidationError
from app.domain.inventory import validate_item
from app.domain.stress import CAPACITIES

RULESET = 'fate-lite-v1'
SKILL_USES = {
    '学术': '运用系统知识、分析自然现象、施展经训练掌握的法术',
    '运动': '奔跑、攀爬、跳跃、改变位置和闪避',
    '盗窃': '开锁、扒窃、拆除警报或绕过实体防护',
    '人脉': '动用熟人关系、打听消息、寻求引荐',
    '工匠': '制造、修理、布置装置和操作复杂工具',
    '欺瞒': '虚张声势、伪装和制造可信的假象',
    '驾驶': '操纵载具、追逐、脱离和规避危险',
    '同理': '判断情绪和意图、安抚他人',
    '战斗': '徒手或近战武器的搏击、格挡和缴械',
    '调查': '带着明确目标仔细检查细节、比对线索和寻找弱点',
    '传说': '辨识怪物与古迹的传闻、运用秘传知识和古老仪式',
    '感知': '迅速察觉气味、动静、突发威胁或暴露的破绽',
    '体魄': '举托、挣脱、承受物理冲击和长时间用力',
    '挑弄': '激怒、威吓或以言行扰乱对方',
    '亲善': '建立信任、说服、调解和鼓舞',
    '资源': '调动钱财、库存、物资和供应渠道',
    '射击': '用弓、枪、投射装置等准确定向攻击',
    '隐匿': '藏身、潜行、藏匿物件和躲开观察',
    '意志': '抗拒恐惧、诱惑、精神影响并维持专注',
}
SKILLS = list(SKILL_USES)
CONFIG = {'ruleset_id': RULESET, 'targets': {'easy':0,'standard':2,'hard':4,'extreme':6},
          'environment_step':1,'tie_succeeds':True}


def validate_character(data):
    if not isinstance(data, dict):
        raise ValidationError('角色卡必须是对象')
    if set(data) - {'name','background','motivation','skill_reason','aspects','skills','stunts','equipment','money','ruleset_id','stress_tracks','consequences'}:
        raise ValidationError('角色卡包含未知字段')
    for key in ('name', 'background', 'motivation', 'skill_reason'):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValidationError('请补充角色字段：' + key)
    aspects = data.get('aspects')
    if not isinstance(aspects, list) or len(aspects) != 2:
        raise ValidationError('初始角色需要两个形象')
    if any(not isinstance(a,dict) for a in aspects):
        raise ValidationError('形象必须是对象')
    if [a.get('kind') for a in aspects] != ['high_concept', 'free']:
        raise ValidationError('形象顺序为高概念、自由形象')
    for a in aspects:
        if not isinstance(a.get('text'), str) or not a['text'].strip():
            raise ValidationError('形象不能为空')
    skills = data.get('skills')
    if not isinstance(skills, dict) or any(k not in SKILLS or type(v) is not int or v not in (0,1,2) for k,v in skills.items()):
        raise ValidationError('技能必须来自目录，等级只能为 0、1、2')
    if sorted(v for v in skills.values() if v) != [1,1,2]:
        raise ValidationError('初始技能必须为一个 +2、两个 +1')
    if data.get('stunts', []) or 'fate_points' in data or 'refresh' in data:
        raise ValidationError('建卡时无初始绝技、命运点或重振')
    if not isinstance(data.get('equipment', []), list):
        raise ValidationError('随身物品必须是列表')
    for entry in data.get('equipment', []):
        validate_item(entry)
    if type(data.get('money', 0)) is not int or data.get('money', 0) < 0:
        raise ValidationError('初始货币必须是非负整数')
    result = deepcopy(data)
    result['aspects'] = [{'id': 'aspect:high_concept' if i == 0 else 'aspect:free', **a} for i,a in enumerate(aspects)]
    # IDs are controlled by the engine, not by model output.
    for i,a in enumerate(result['aspects']): a['id'] = ('aspect:high_concept', 'aspect:free')[i]
    result['skills'] = {s: skills.get(s,0) for s in SKILLS}
    result['stunts'] = []
    result['ruleset_id'] = RULESET
    result['stress_tracks'] = {'unified': [False] * 3}
    result['consequences'] = {name: {'capacity':n,'text':None} for name,n in CAPACITIES.items()}
    return result
