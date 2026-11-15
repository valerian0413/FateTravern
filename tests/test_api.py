import asyncio
import json
from dataclasses import replace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.api.server import create_app
from app.bootstrap import build_runtime
from app.config import Settings
from app.providers.base import ModelResponse


@pytest.fixture
async def client(tmp_path, settings: Settings):
    settings = replace(settings, db_path=tmp_path / 'api.sqlite3', log_dir=tmp_path / 'logs')
    runtime = await build_runtime(settings, require_model=False, fixtures={
        'intent': {
            'kind': 'discussion', 'normalized_action': None, 'target_ids': [],
            'creation_statements': [], 'needs_clarification': False, 'question': None,
            'requested_tools': [], 'unresolved': [],
        },
        'discussion': {'reply': '可以尝试，公开环境下风向大致可判断。', 'suggested_action': '尝试判断风向', 'can_execute': True},
    })
    app = create_app(runtime)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    yield client, runtime
    await client.close()
    await runtime.worker.stop()
    await runtime.db.close()


async def test_create_and_discuss(client):
    client, runtime = client
    res = await client.post('/api/v1/worlds', json={'preset_id': 'sky-islands-v1'})
    assert res.status == 201
    world = await res.json()
    wid = world['id']

    res = await client.post(f'/api/v1/worlds/{wid}/messages', json={
        'request_id': 'disc-1', 'text': '我能用自然知识判断风向吗', 'mode_hint': '交流',
    })
    assert res.status == 202
    for _ in range(50):
        status = await client.get(f'/api/v1/worlds/{wid}/requests/disc-1')
        body = await status.json()
        if body['status'] == 'completed':
            break
        await asyncio.sleep(0.05)
    assert body['status'] == 'completed'
    msgs = await (await client.get(f'/api/v1/worlds/{wid}/messages?channel=interaction')).json()
    assert any(m['author'] == 'gm' for m in msgs)
    view = await (await client.get(f'/api/v1/worlds/{wid}/view')).json()
    assert view['version'] == 0  # discussion does not advance world


async def test_workshop_http_persistence_and_validation(client):
    http, runtime = client
    response = await http.post('/api/v1/workshop', json={})
    assert response.status == 201
    draft = await response.json()
    loaded = await (await http.get('/api/v1/workshop/'+draft['id'])).json()
    assert loaded == draft
    rows = await (await http.get('/api/v1/workshop')).json()
    assert rows[0]['id'] == draft['id']
    response = await http.post('/api/v1/workshop/'+draft['id']+'/confirm',
                               json={'revision':0,'action':'confirm_world'})
    assert response.status == 400  # Missing chapters/initial scene cannot launch.
    assert not await (await http.get('/api/v1/worlds')).json()
    response = await http.post('/api/v1/workshop/'+draft['id']+'/messages', json=[])
    assert response.status == 400


async def test_world_setting_catalog_and_copy_http(client):
    client, _ = client
    response = await client.get('/api/v1/workshop/catalog')
    assert response.status == 200
    catalog = await response.json()
    assert any(item['id'] == 'sky-islands-v1' for item in catalog['presets'])
    response = await client.post('/api/v1/workshop', json={'source': {'kind': 'preset', 'id': 'sky-islands-v1'}})
    assert response.status == 201
    draft = await response.json()
    response = await client.post(f"/api/v1/workshop/{draft['id']}/confirm", json={'revision': 0, 'action': 'confirm_world'})
    assert response.status == 200
    catalog = await (await client.get('/api/v1/workshop/catalog')).json()
    assert any(item['id'] == draft['id'] for item in catalog['saved'])
    response = await client.post('/api/v1/workshop', json={'source': {'kind': 'saved', 'id': draft['id']}})
    copied = await response.json()
    assert response.status == 201 and copied['stage'] == 'world'
    assert copied['id'] != draft['id']
    result = await client.delete(f"/api/v1/workshop/catalog/saved/{draft['id']}")
    assert result.status == 200
    assert (await client.get(f"/api/v1/workshop/{draft['id']}")).status == 404
    assert (await client.get(f"/api/v1/workshop/{copied['id']}")).status == 200


class OpeningModel:
    def __init__(self):
        self.calls = 0

    async def complete(self, request):
        assert request.module == 'opening'
        self.calls += 1
        context = json.loads(request.messages[1]['content'])
        assert context['scene']['location'] == '雷云层中的飞艇'
        assert '见习学者' in context['character']['identity']
        return ModelResponse({
            'background': '当年，残破的飞艇冲出雷云，在风栈岛落脚。如今冒险者从那里飞向云海深处。',
            'character': '你是见习学者云隼，想完成老师留下的《空岛怪物指南》。',
            'scene': '你搭乘的飞艇正穿过雷云层。一只云鲨掉头直冲船身。',
            'question': '你怎么护住飞艇？',
            'options': ['我提醒船上的人留意云鲨', '我举起旧法器，试着引开云鲨'],
        }, '', {})


async def test_demo_route_copies_xuan_yun_hai(client, monkeypatch):
    http, _runtime = client
    listed = await (await http.get('/api/v1/demos')).json()
    assert any(item['id'] == 'xuan-yun-hai' and item['character_name'] == '云隼·见习学者' for item in listed)
    created = await http.post('/api/v1/demos/xuan-yun-hai/play', json={})
    assert created.status == 201
    body = await created.json()
    assert body['name'] == '悬云海-预设' and body['phase'] == 'prepared'
    path = f"/api/v1/worlds/{body['id']}"
    unavailable = await http.post(f'{path}/start', json={'request_id': 'demo-opening'})
    assert unavailable.status == 502
    assert (await unavailable.json())['code'] == 'model_unavailable'
    assert not await (await http.get(f'{path}/messages?channel=narrative')).json()
    assert (await (await http.get(f'{path}/view')).json())['phase'] == 'prepared'

    model = OpeningModel()
    with monkeypatch.context() as patch:
        patch.setattr(_runtime, 'provider', model)
        opened = await http.post(f'{path}/start', json={'request_id': 'demo-opening'})
        assert opened.status == 202
        repeated = await http.post(f'{path}/start', json={'request_id': 'repeat-opening'})
        assert repeated.status == 202
        assert model.calls == 1
    narrative = await (await http.get(f'{path}/messages?channel=narrative')).json()
    assert len(narrative) == 1 and narrative[0]['author'] == 'gm'
    opening = narrative[0]['text']
    background, character, scene = opening.split('\n\n')
    assert '风栈岛' in background
    assert '见习学者' in character and '《空岛怪物指南》' in character
    assert '云鲨' in scene
    initial = await (await http.get(f'{path}/view')).json()
    assert initial['version'] == initial['clock'] == 0
    assert any(item['name'] == '铜币' and item['quantity'] == 5 for item in initial['character']['inventory'])
    interaction = await (await http.get(f'{path}/messages?channel=interaction')).json()
    assert interaction[0]['choices_active']
    options = interaction[0]['options']
    assert options == ['我提醒船上的人留意云鲨', '我举起旧法器，试着引开云鲨']
    result = await http.post(f'{path}/messages', json={
        'request_id': 'demo-first-choice', 'text': options[0], 'reply_to': 'demo-opening',
    })
    assert result.status == 202
    task = _runtime.chat._tasks.get((body['id'], 'demo-first-choice'))
    if task:
        await task
    view = await (await http.get(f'{path}/view')).json()
    assert view['version'] == 1
    interaction = await (await http.get(f'{path}/messages?channel=interaction')).json()
    assert sum(row['author'] == 'gm' and row['text'].startswith('好，你试着') for row in interaction) == 1
    missing = await http.post('/api/v1/demos/missing/play', json={})
    assert missing.status == 404


async def test_delete_world_setting_keeps_adventure_and_protects_preset(client):
    client, _ = client
    preset = 'xuan-yun-hai-v1'
    result = await client.delete(f'/api/v1/workshop/catalog/preset/{preset}')
    assert result.status == 400

    result = await client.post('/api/v1/worlds', json={'preset_id': preset})
    assert result.status == 201
    world = await result.json()
    world_id = world['id']
    result = await client.delete(f'/api/v1/workshop/catalog/archive/{world_id}')
    assert result.status == 200
    catalog = await (await client.get('/api/v1/workshop/catalog')).json()
    assert all(item['id'] != world_id for item in catalog['saved'])
    assert any(item['id'] == preset for item in catalog['presets'])
    assert (await client.get(f'/api/v1/worlds/{world_id}/view')).status == 200
    result = await client.post('/api/v1/workshop', json={'source': {'kind': 'archive', 'id': world_id}})
    assert result.status == 404


async def test_interaction_retry_keeps_failed_consultation_out_of_adventure(client, monkeypatch):
    http, runtime = client
    created = await http.post('/api/v1/worlds', json={'preset_id': 'sky-islands-v1'})
    world_id = (await created.json())['id']
    path = f'/api/v1/worlds/{world_id}'
    original = runtime.chat.discuss

    async def unavailable(context, question):
        raise RuntimeError('temporary model failure')

    monkeypatch.setattr(runtime.chat, 'discuss', unavailable)
    submitted = await http.post(f'{path}/messages', json={'request_id': 'ask', 'text': '可以确定风向吗？'})
    assert submitted.status == 202
    task = runtime.chat._tasks.get((world_id, 'ask'))
    if task:
        await task
    status = await (await http.get(f'{path}/requests/ask')).json()
    assert status['status'] == 'failed_retryable'

    monkeypatch.setattr(runtime.chat, 'discuss', original)
    response = await http.post(f'{path}/requests/ask/retry', json={})
    assert response.status == 202
    task = runtime.chat._tasks.get((world_id, 'ask'))
    if task:
        await task
    status = await (await http.get(f'{path}/requests/ask')).json()
    assert status['result']['kind'] == 'discussion'
    messages = await (await http.get(f'{path}/messages?channel=interaction')).json()
    assert [message['author'] for message in messages] == ['player', 'gm']
    assert not await (await http.get(f'{path}/messages?channel=narrative')).json()
    assert (await (await http.get(f'{path}/view')).json())['version'] == 0


async def test_native_lookup_returns_data_and_model_selects_host_window(client):
    import json

    from app.providers.base import ModelResponse
    from app.storage.repositories import MessageRepo
    from app.domain.types import Channel

    http, runtime = client
    created = await http.post('/api/v1/worlds', json={'preset_id': 'sky-islands-v1'})
    world_id = (await created.json())['id']
    path = f'/api/v1/worlds/{world_id}'
    async with runtime.db.transaction():
        await MessageRepo(runtime.db.conn).append(world_id, Channel.NARRATIVE, 'gm', '旧剧情提到铜铃。')

    class Model:
        calls = 0

        async def complete(self, request):
            self.calls += 1
            assert request.module == 'host_tools'
            assert request.tools
            if self.calls == 1:
                name, args = 'lookup_public', {'query': '铜铃'}
            elif self.calls == 2:
                result = request.messages[-1]
                assert result['role'] == 'tool'
                assert result['tool_call_id'] == 'step-1'
                assert '旧剧情提到铜铃' in result['content']
                name, args = 'reply_to_host', {'reply': '你见过一枚铜铃。'}
            else:
                pytest.fail('咨询工具应直接输出，不能再调用回复模型')
            return ModelResponse(None, '', {}, message={'role': 'assistant', 'content': None, 'tool_calls': [{
                'id': f'step-{self.calls}', 'type': 'function',
                'function': {'name': name, 'arguments': json.dumps(args)},
            }]})

        async def close(self):
            pass

    runtime.fixtures.pop('intent')
    runtime.provider = Model()
    response = await http.post(f'{path}/messages', json={'request_id': 'native', 'text': '以前见过铜铃吗？'})
    assert response.status == 202
    task = runtime.chat._tasks.get((world_id, 'native'))
    if task:
        await task
    status = await (await http.get(f'{path}/requests/native')).json()
    assert status['result']['reply'] == '你见过一枚铜铃。'
    narrative = await (await http.get(f'{path}/messages?channel=narrative')).json()
    assert [message['text'] for message in narrative] == ['旧剧情提到铜铃。']
    assert (await (await http.get(f'{path}/view')).json())['version'] == 0


async def test_delete_adventure_removes_only_its_records(client, monkeypatch):
    http, runtime = client
    response = await http.post('/api/v1/demos/xuan-yun-hai/play', json={})
    world_id = (await response.json())['id']
    other = await http.post('/api/v1/demos/xuan-yun-hai/play', json={})
    other_id = (await other.json())['id']
    path = f'/api/v1/worlds/{world_id}'
    with monkeypatch.context() as patch:
        patch.setattr(runtime, 'provider', OpeningModel())
        assert (await http.post(f'{path}/start', json={'request_id': 'opening'})).status == 202
    assert (await http.delete(path)).status == 200
    assert (await http.get(f'{path}/view')).status == 404
    assert (await http.get(f'/api/v1/worlds/{other_id}/view')).status == 200
    for table in ('entities', 'facts', 'messages', 'events', 'turns', 'stream_events'):
        assert not await runtime.db.fetchall(f'SELECT 1 FROM {table} WHERE world_id=?', (world_id,))
    assert not await runtime.db.fetchall('PRAGMA foreign_key_check')
