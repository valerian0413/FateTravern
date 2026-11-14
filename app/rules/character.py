"""Versioned simplified Fate character creation and damage data."""
from copy import deepcopy
from app.domain.errors import ValidationError
from app.domain.stress import CAPACITIES

RULESET = 'fate-lite-v1'
SKILLS = ['学术','运动','盗窃','人脉','工匠','欺瞒','驾驶','同理','战斗','调查','传说','感知','体魄','挑弄','亲善','资源','射击','隐匿','意志']
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
    if not isinstance(data.get('equipment', []), list) or any(not isinstance(e,str) for e in data.get('equipment', [])):
        raise ValidationError('装备必须是名称列表')
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
