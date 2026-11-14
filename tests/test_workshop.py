from copy import deepcopy
from dataclasses import replace
import pytest
from app.application import workshop
from app.bootstrap import build_runtime
from app.domain.errors import Conflict, ValidationError
from app.domain.changes import apply_to_copy
from app.providers.base import ModelResponse
from app.rules.character import validate_character, CONFIG, SKILLS
from app.rules.four_dice import FourDice
from app.storage.repositories import WorldRepo


def character():
    return {'name':'小剑士','background':'港口长大，练习剑术','motivation':'寻找失踪船长',
            'skill_reason':'剑术训练与港口生活',
            'aspects':[{'kind':'high_concept','text':'空港初行剑士'},{'kind':'free','text':'熟悉港口的人情'}],
            'skills':{'战斗':2,'体魄':1,'人脉':1},'stunts':[], 'equipment':['短剑'],'money':3}


def book():
    return {'schema_version':1,'name':'测试群岛','skills':SKILLS,
            'sections':{s:[{'id':s,'text':s+' 测试设定','visibility':'public','status':'proposed','mutability':'fixed'}]
                        for s in workshop.SECTIONS},
            'initial_scene':{'name':'港口','description':'木栈桥上有一个布告板','opening':'我环顾港口'}}


@pytest.fixture
async def runtime(settings,tmp_path):
    rt=await build_runtime(replace(settings,db_path=tmp_path/'draft.sqlite3'),require_model=False)
    await rt.worker.stop()
    yield rt
    await rt.db.close()


def fake(rt, document, conflicts=None, intent="edit"):
    class Provider:
        async def complete(self, request):
            assert request.module=='workshop'
            return ModelResponse({'intent':intent, 'reply':'请确认或继续修改','document':deepcopy(document),
                                  'options':['继续完善','保留当前设定'],'conflicts':conflicts or []},'',{})
    rt.provider=Provider()


async def test_create_confirm_and_launch(runtime):
    rt=runtime
    draft=await workshop.create(rt)
    fake(rt,book())
    draft=await workshop.message(rt,draft['id'],{'revision':0,'text':'设计浮空群岛'})
    assert not await WorldRepo(rt.db.conn).list()
    draft=await workshop.confirm(rt,draft['id'],{'revision':1,'action':'confirm_world'})
    assert draft['stage']=='character'
    fake(rt,character())
    draft=await workshop.message(rt,draft['id'],{'revision':2,'text':'我是港口的小剑士'})
    draft=await workshop.confirm(rt,draft['id'],{'revision':3,'action':'confirm_character'})
    snap=await WorldRepo(rt.db.conn).get_snapshot(draft['world_id'])
    assert snap.world.ruleset_id=='fate-lite-v1'
    attrs=snap.entities[snap.world.player_id].attributes
    assert attrs['stress_tracks']=={'unified':[False]*3}
    assert 'hp' not in attrs['resources'] and 'stress' not in attrs['resources']
    assert len(attrs['aspects'])==2 and attrs['stunts']==[]
    assert snap.world.ruleset_config['world_book']['revision']==2
    again=await workshop.confirm(rt,draft['id'],{'revision':4,'action':'confirm_character'})
    assert again['world_id']==draft['world_id']
    assert len(await WorldRepo(rt.db.conn).list())==1


async def test_form_edit_saves_without_model(runtime):
    draft = await workshop.create(runtime)
    edited = deepcopy(draft['book'])
    edited['name'] = '悬云海'
    edited['sections']['supernatural'] = [{
        'id': 'no-magic', 'text': '没有超凡。', 'visibility': 'public',
        'status': 'proposed', 'mutability': 'fixed',
    }]
    saved = await workshop.update_document(runtime, draft['id'], {'revision': 0, 'book': edited})
    assert saved['book']['name'] == '悬云海'
    assert saved['book']['sections']['supernatural'][0]['text'] == '没有超凡。'
    assert saved['revision'] == 1
    with pytest.raises(Conflict):
        await workshop.update_document(runtime, draft['id'], {'revision': 0, 'book': edited})


async def test_form_cannot_overwrite_confirmed_entry(runtime):
    draft = await workshop.create(runtime)
    edited = deepcopy(book())
    edited['sections']['currency'][0]['status'] = 'confirmed'
    saved = await workshop.update_document(runtime, draft['id'], {'revision': 0, 'book': edited})
    changed = deepcopy(saved['book'])
    changed['sections']['currency'][0]['text'] = '改掉已确认货币'
    with pytest.raises(Conflict):
        await workshop.update_document(runtime, draft['id'], {'revision': saved['revision'], 'book': changed})


async def test_character_form_can_save_partial(runtime):
    draft = await workshop.create(runtime)
    draft = await workshop.update_document(runtime, draft['id'], {'revision': 0, 'book': book()})
    draft = await workshop.confirm(runtime, draft['id'], {'revision': draft['revision'], 'action': 'confirm_world'})
    partial = {
        'name': '旅人', 'background': '', 'motivation': '', 'skill_reason': '',
        'aspects': [{'kind': 'high_concept', 'text': ''}, {'kind': 'free', 'text': ''}],
        'skills': {}, 'stunts': [], 'equipment': [], 'money': 0,
    }
    saved = await workshop.update_document(runtime, draft['id'], {'revision': draft['revision'], 'character': partial})
    assert saved['stage'] == 'character' and saved['character']['name'] == '旅人'
    with pytest.raises(ValidationError):
        await workshop.confirm(runtime, draft['id'], {'revision': saved['revision'], 'action': 'confirm_character'})
    partial['unknown'] = True
    with pytest.raises(ValidationError):
        await workshop.update_document(runtime, draft['id'], {'revision': saved['revision'], 'character': partial})


async def test_conflict_is_pending_and_stale_revision_rejected(runtime):
    draft=await workshop.create(runtime)
    fake(runtime,book(),['空岛设定与海底都市冲突'])
    result=await workshop.message(runtime,draft['id'],{'revision':0,'text':'修改世界'})
    assert result['book']['name']=='' and result['pending']
    with pytest.raises(Conflict):
        await workshop.confirm(runtime,draft['id'],{'revision':0,'action':'accept_change'})
    result=await workshop.confirm(runtime,draft['id'],{'revision':1,'action':'accept_change'})
    assert result['book']['name']=='测试群岛'


@pytest.mark.parametrize('skills',[{'战斗':4},{'战斗':2,'体魄':2,'人脉':1},{'剑术':2,'体魄':1,'人脉':1}])
def test_invalid_skill_allocation(skills):
    card=character();card['skills']=skills
    with pytest.raises(ValidationError):validate_character(card)


def test_aspect_conditions_and_binary_results():
    card=validate_character(character())
    rules=FourDice(CONFIG)
    c={'mode':'check','domain':'人脉','difficulty':'standard','conditions':[
        {'source_id':'aspect:free','basis_id':'knows-dockmaster','direction':'advantage','reason':'与港务熟识'}]}
    a=rules.assess(c,card)
    r=rules.resolve(a,{'dice':[0,0,0,0]})
    assert r['total']==2 and r['dc']==2 and r['success'] is True
    assert r['ruleset_version']=='fate-lite-v1'
    c['conditions'].append({'source_id':'environment','basis_id':'knows-dockmaster','direction':'advantage'})
    with pytest.raises(ValidationError):rules.assess(c,card)
    c['conditions']=[{'source_id':'aspect:missing','direction':'advantage','basis_id':'x','reason':'x'}]
    with pytest.raises(ValidationError):rules.assess(c,card)


def test_stress_consequence_state(world_snapshot):
    snap=world_snapshot
    snap.world.ruleset_id='fate-lite-v1'
    pid=snap.world.player_id
    snap.entities[pid].attributes.update(validate_character(character()))
    op={'op':'mark_stress','op_id':'hit','entity_id':pid,'boxes':[1]}
    state=apply_to_copy(snap,[op])
    assert state.entities[pid].attributes['stress_tracks']['unified']==[False,True,False]
    assert not any(snap.entities[pid].attributes['stress_tracks']['unified'])
    with pytest.raises(ValidationError):apply_to_copy(state,[op])
    state=apply_to_copy(state,[{'op':'take_consequence','op_id':'wound','entity_id':pid,'slot':'mild','text':'手臂擦伤'}])
    state=apply_to_copy(state,[{'op':'clear_stress','op_id':'rest','entity_id':pid,'reason':'冲突结束，安全喘息'}])
    assert not any(state.entities[pid].attributes['stress_tracks']['unified'])
    assert state.entities[pid].attributes['consequences']['mild']['text'] is None


async def test_saved_setting_reusable_before_character_and_isolated(runtime):
    draft = await workshop.create(runtime)
    draft = await workshop.update_document(runtime, draft['id'], {'revision': 0, 'book': book()})
    saved = await workshop.confirm(runtime, draft['id'], {'revision': draft['revision'], 'action': 'confirm_world'})
    catalog = await workshop.catalog(runtime)
    assert any(item['id'] == saved['id'] for item in catalog['saved'])
    assert not await WorldRepo(runtime.db.conn).list()
    assert len(saved['options']) >= 2
    copied = await workshop.create(runtime, {'kind': 'saved', 'id': saved['id']})
    changed = deepcopy(copied['book'])
    changed['sections']['background'][0]['text'] = '自由修改后的世界'
    changed['sections']['currency'] = []
    await workshop.update_document(runtime, copied['id'], {'revision': 0, 'book': changed})
    original = await workshop.get(runtime, saved['id'])
    assert original['book']['sections']['background'][0]['text'] != '自由修改后的世界'
    assert original['book']['sections']['currency']
    assert copied['character'] is None and copied['world_id'] is None


async def test_preset_can_be_saved_and_draft_cannot_be_used_as_setting(runtime):
    draft = await workshop.create(runtime, {'kind': 'preset', 'id': 'sky-islands-v1'})
    workshop.validate_book(draft['book'], complete=True)
    assert draft['book']['name'] == '浮空群岛'
    with pytest.raises(Conflict):
        await workshop.create(runtime, {'kind': 'saved', 'id': draft['id']})
    result = await workshop.confirm(runtime, draft['id'], {'revision': 0, 'action': 'confirm_world'})
    assert result['stage'] == 'character'


@pytest.mark.parametrize('missing,conflicts', [(False, []), (True, []), (False, ['开场地点冲突'])])
async def test_finish_world_in_chat(runtime, missing, conflicts):
    draft = await workshop.create(runtime)
    candidate = book()
    candidate['sections']['transport'] = []
    if missing:
        candidate['initial_scene']['name'] = ''
    fake(runtime, candidate, conflicts, intent='finish_world')
    text = '现有内容已足够，直接创建世界并进入角色阶段'
    result = await workshop.message(runtime, draft['id'], {'revision': 0, 'text': text})
    assert result == await workshop.get(runtime, draft['id'])
    if conflicts:
        assert result['stage'] == 'world' and result['pending']
        result = await workshop.confirm(runtime, draft['id'], {'revision': 1, 'action': 'accept_change'})
    if missing:
        assert result['stage'] == 'world'
        assert '开场地点' in result['history'][-1]['text']
    else:
        assert result['stage'] == 'character'
        assert result['character'] is None
        assert {'role': 'player', 'text': text} in result['world_history']
        assert len(result['history']) == 1


async def test_deleting_adventure_keeps_confirmed_world_setting(runtime):
    from app.application.archives import delete_archive

    draft = await workshop.create(runtime)
    draft = await workshop.update_document(runtime, draft['id'], {'revision': 0, 'book': book()})
    draft = await workshop.confirm(runtime, draft['id'], {'revision': 1, 'action': 'confirm_world'})
    draft = await workshop.update_document(runtime, draft['id'], {'revision': 2, 'character': character()})
    draft = await workshop.confirm(runtime, draft['id'], {'revision': 3, 'action': 'confirm_character'})
    await delete_archive(runtime, draft['world_id'])
    saved = await workshop.get(runtime, draft['id'])
    assert saved['stage'] == 'saved' and saved['character'] is None and saved['world_id'] is None
    assert saved['book'] == draft['book']
    copy = await workshop.create(runtime, {'kind': 'saved', 'id': saved['id']})
    assert copy['book']['name'] == draft['book']['name']
