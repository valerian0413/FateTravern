"""Regression for NPC dialogue in OOC channel and repeated NPC generation."""
import pytest

from test_action_flow import game, plan, send  # noqa: F401  game is a pytest fixture
from app.application.context import build_world_context
from app.application.entities import context_ids, relevant_ids, reconcile
from app.domain.types import Entity, EntityKind
from app.domain.errors import ValidationError, StaleTurn
from app.modules.intent import _to_intent
from app.modules.planning import validate_plan
from app.storage.repositories import EntityRepo, MessageRepo, TurnRepo, WorldRepo


def test_relevant_npc_survives_context_limit(world_snapshot):
    snap=world_snapshot
    for i in range(40):
        snap.entities[f'item-{i}']=Entity(id=f'item-{i}',world_id=snap.world.id,kind=EntityKind.ITEM,name=f'行李{i}',attributes={},revision=1)
    snap.entities['kor']=Entity(id='kor',world_id=snap.world.id,kind=EntityKind.NPC,name='柯尔',attributes={'aliases':['老柯']},revision=1)
    history=[{'text':'柯尔提出明早出发。'}]
    assert relevant_ids(snap,'我与他约定明日出发',['wrong-id'],history)==['kor']
    assert relevant_ids(snap,'询问老柯',['柯尔'])==['kor']
    assert context_ids(snap,'询问柯尔')[0]=='kor'


async def test_context_entity_and_fact_priority(world_snapshot):
    snap=world_snapshot
    snap.entities['kor']=Entity(id='kor',world_id=snap.world.id,kind=EntityKind.NPC,name='柯尔',attributes={},revision=1)
    result=await build_world_context(snap,['kor',*snap.entities],budget=1)
    assert result['entities'][0]['id']=='kor'


def test_only_world_fact_gaps_trigger_generation():
    intent=_to_intent({'kind':'explore','requested_tools':['generation'], 'unresolved':[
        {'kind':'npc_response','detail':'是否答应分成'}, {'kind':'player_choice','detail':'是否明早出发'}]})
    assert intent.unresolved==[] and 'generation' not in intent.requested_tools
    intent=_to_intent({'kind':'explore','unresolved':[{'kind':'world_fact','detail':'未知悬崖地形'}]})
    assert intent.unresolved==['未知悬崖地形']


def test_duplicate_candidate_reuses_identity_and_rewrites_facts(world_snapshot):
    snap=world_snapshot
    snap.entities['kor']=Entity(id='kor',world_id=snap.world.id,kind=EntityKind.NPC,name='柯尔',attributes={},revision=1)
    batch={'temp_entities':[{'id':'new:kor','kind':'npc','name':'柯尔'}],
           'facts':[{'id':'new:f','subject_id':'new:kor','predicate':'reported','value':'上次绳子不够长'}]}
    result=reconcile(batch,snap,['kor'])
    assert not result['temp_entities']
    assert result['facts'][0]['subject_id']=='kor'
    assert batch['facts'][0]['subject_id']=='new:kor'
    with pytest.raises(ValidationError):reconcile(batch,snap,[])
    snap.entities['kor2']=Entity(id='kor2',world_id=snap.world.id,kind=EntityKind.NPC,name='柯尔',attributes={},revision=1)
    with pytest.raises(ValidationError):reconcile(batch,snap,['kor'])


async def test_after_action_commits_npc_reply_then_shows_choice(game):
    rt,world=game
    rt.fixtures['planning']=plan(needs_choice=True,choice_timing='after_action',
                                prompt='是否接受明早同行的安排？',options=['接受明早同行','暂不答应'])
    rt.fixtures['narration']={'player_action_text':'我询问上次的困难。',
                              'gm_text':'柯尔说上次绳子不够长，提出明早碰头。'}
    rt.fixtures['planning']['challenge'].update(mode='automatic',duration_minutes=2,
        success_effects=[{'op':'append_fact','op_id':'report','subject_id':world.player_id,
                          'predicate':'heard','value':'柯尔提议明早碰头','disclosure_rule':{'disclosure':'public'}}])
    turn=await send(rt,world.id,'after')
    assert turn.stage.value=='completed',turn.checkpoint
    assert turn.result['stop_reason']=='new_choice'
    snap=await WorldRepo(rt.db.conn).get_snapshot(world.id)
    assert snap.world.version==world.version+1
    assert any(f.value=='柯尔提议明早碰头' for f in snap.facts.values())
    narrative=await MessageRepo(rt.db.conn).list(world.id,'narrative')
    interaction=await MessageRepo(rt.db.conn).list(world.id,'interaction')
    assert any('绳子不够长' in m['text'] for m in narrative)
    assert not any('绳子不够长' in m['text'] for m in interaction)
    assert interaction[-1]['options']==['接受明早同行','暂不答应']


def test_npc_monologue_rejected_as_choice_prompt():
    with pytest.raises(ValidationError,match='NPC 台词'):
        validate_plan(plan(needs_choice=True,prompt='柯尔说：『明天见。』',options=['答应','拒绝']),{})


async def test_old_duplicate_preparation_replanned_without_reroll(game):
    rt,world=game
    async with rt.db.transaction():
        await EntityRepo(rt.db.conn).upsert(Entity(id='kor',world_id=world.id,kind=EntityKind.NPC,name='柯尔',attributes={},revision=1))
    # First create a retryable failure, then model the old on-disk duplicate preparation.
    rt.fixtures['planning']['challenge']['success_effects']=[{'op':'bogus','op_id':'bad'}]
    turn=await send(rt,world.id,'old','与柯尔约定明日出发')
    assert turn.stage.value=='failed_retryable'
    turn.checkpoint['prepared']={'registered':{'temp_entities':[{'id':'old-copy','kind':'npc','name':'柯尔'}]},'plan':plan()}
    await TurnRepo(rt.db.conn).checkpoint(world.id,turn.id,turn.stage,turn.checkpoint)
    await rt.db.conn.commit()
    rt.fixtures['planning']=plan()
    result=await rt.chat.turns.resume(turn.id,world.id)
    assert result.stage=='completed'
    snap=await WorldRepo(rt.db.conn).get_snapshot(world.id)
    assert [e.id for e in snap.entities.values() if e.name=='柯尔']==['kor']


@pytest.mark.parametrize('dice', [[], [1,0,-1,1]])
async def test_duplicate_retry_preserves_real_rolls(game,dice):
    from app.storage.repositories import CheckRepo
    rt,world=game
    async with rt.db.transaction():
        await EntityRepo(rt.db.conn).upsert(Entity(id='kor',world_id=world.id,kind=EntityKind.NPC,name='柯尔',attributes={},revision=1))
    rt.fixtures['planning']['challenge']['success_effects']=[{'op':'bogus','op_id':'bad'}]
    turn=await send(rt,world.id,'old-check','与柯尔约定明日出发')
    turn.checkpoint['prepared']={'registered':{'temp_entities':[{'id':'copy','kind':'npc','name':'柯尔'}]},'plan':plan()}
    cid=f'{world.id}:old-check:0'
    async with rt.db.transaction():
        await TurnRepo(rt.db.conn).checkpoint(world.id,turn.id,turn.stage,turn.checkpoint)
        await CheckRepo(rt.db.conn).save(world.id,{'check_id':cid,'turn_id':turn.id,'input_hash':'old',
            'ruleset_version':'four-dice-v1','dice':dice,'payload':{'dice':dice}})
    rt.fixtures['planning']=plan()
    if dice:
        with pytest.raises(StaleTurn,match='已经掷骰'):
            await rt.chat.turns.resume(turn.id,world.id)
        saved=await CheckRepo(rt.db.conn).get(world.id,cid)
        assert saved['dice']==dice and saved['input_hash']=='old'
    else:
        result=await rt.chat.turns.resume(turn.id,world.id)
        assert result.stage=='completed'
        saved=await CheckRepo(rt.db.conn).get(world.id,cid)
        assert saved['input_hash']!='old'
