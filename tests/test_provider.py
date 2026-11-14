"""Request-layer regressions; no commercial API or game-state writes."""
import asyncio
import json
from dataclasses import replace

import pytest

from app.config import load_settings, validate_settings
from app.domain.errors import ModelRequestError
from app.providers.base import ModelRequest
from app.providers.chat_completions import ChatCompletionsProvider


def request(**kwargs):
    return ModelRequest(module='test', prompt_version='1', schema_version='1',
                        world_id=None, turn_id=None, call_id='test-call',
                        messages=[{'role': 'system', 'content': '返回 {"ok":true}'}], **kwargs)


def completion(content='{"ok":true}', reason='stop', **message):
    return {'id': 'response-1', 'model': 'test-model',
            'choices': [{'finish_reason': reason, 'message': {
                'role': 'assistant', 'content': content, **message}}],
            'usage': {'total_tokens': 42, 'prompt_cache_hit_tokens': 10}}


class Response:
    def __init__(self, status, payload):
        self.status, self.payload = status, payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def text(self):
        if isinstance(self.payload, str):
            return self.payload
        return json.dumps(self.payload)


class Session:
    closed = False

    def __init__(self, replies):
        self.replies = iter(replies)
        self.bodies = []

    def post(self, endpoint, **kwargs):
        self.bodies.append(kwargs['json'])
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return Response(*reply)


@pytest.fixture
def provider(settings, monkeypatch):
    async def no_wait(_):
        pass
    monkeypatch.setattr('app.providers.chat_completions.asyncio.sleep', no_wait)
    return ChatCompletionsProvider(settings)


def logs(provider):
    return [json.loads(line) for line in provider.trace.path.read_text().splitlines()]


def test_request_policy(settings):
    provider = ChatCompletionsProvider(replace(settings, llm_base_url='https://api.deepseek.com'))
    req = request(output_budget=256)
    body = provider.build_body(req)
    assert body['response_format'] == {'type': 'json_object'}
    assert body['max_tokens'] == 256
    assert body['thinking'] == {'type': 'disabled'}
    assert 'reasoning_effort' not in body
    assert 'JSON' in body['messages'][0]['content']
    assert req.messages[0]['content'] == '返回 {"ok":true}'
    body = provider.build_body(request(thinking='enabled', reasoning_effort='high'))
    assert body['reasoning_effort'] == 'high'
    assert 'thinking' not in ChatCompletionsProvider(settings).build_body(req)


async def test_success_preserves_message_usage(provider):
    provider._session = Session([(200, completion(reasoning_content='reasoning retained'))])
    result = await provider.complete(request())
    assert result.content == {'ok': True}
    assert result.message['reasoning_content'] == 'reasoning retained'
    assert result.finish_reason == 'stop' and result.response_id == 'response-1'
    records = logs(provider)
    assert len(records) == 2
    assert records[-1]['usage']['prompt_cache_hit_tokens'] == 10
    assert records[-1]['error'] is None
    assert 'Authorization' not in provider.trace.path.read_text()


@pytest.mark.parametrize('status', [400, 401, 402, 403, 404])
async def test_permanent_http_errors_not_retried(provider, status):
    provider._session = Session([(status, {'error': 'bad request'})])
    with pytest.raises(ModelRequestError) as error:
        await provider.complete(request())
    assert error.value.code == f'model_http_{status}'
    assert not error.value.retryable
    assert len(provider._session.bodies) == 1
    assert not logs(provider)[-1]['will_retry']


@pytest.mark.parametrize('first', [(429, {}), (503, {}), asyncio.TimeoutError()])
async def test_transient_failure_then_success_logged_once(provider, first):
    provider._session = Session([first, (200, completion())])
    result = await provider.complete(request())
    assert result.timings['attempts'] == 2
    records = logs(provider)
    assert [r['event'] for r in records] == ['request', 'response', 'request', 'response']
    assert records[1]['will_retry'] and records[-1]['error'] is None
    assert result.timings['total_elapsed_ms'] >= result.timings['elapsed_ms']


@pytest.mark.parametrize('content', ['   ', None, 'not json', '[]', 'true'])
async def test_bad_output_retry_is_bounded(provider, content):
    provider._session = Session([(200, completion(content))] * 3)
    with pytest.raises(ModelRequestError):
        await provider.complete(request())
    assert len(provider._session.bodies) == 2
    assert len(logs(provider)) == 4


async def test_empty_then_valid(provider):
    provider._session = Session([(200, completion(' ')), (200, completion())])
    assert (await provider.complete(request())).content['ok'] is True


@pytest.mark.parametrize('reason,code', [
    ('length', 'model_output_truncated'), ('content_filter', 'model_content_filtered'),
    ('tool_calls', 'model_unexpected_tool_call'), (None, 'model_finish_reason_invalid'),
])
async def test_non_success_finish_is_not_parsed_as_success(provider, reason, code):
    provider._session = Session([(200, completion(reason=reason))])
    with pytest.raises(ModelRequestError) as error:
        await provider.complete(request())
    assert error.value.code == code
    assert len(provider._session.bodies) == 1


@pytest.mark.parametrize('payload', [None, [], {}, {'choices': []}, {'choices': [None]}])
async def test_malformed_envelope(provider, payload):
    provider._session = Session([(200, payload)])
    with pytest.raises(ModelRequestError, match='choices'):
        await provider.complete(request())


async def test_transient_attempt_cap(provider):
    provider._session = Session([(503, {})] * 3)
    with pytest.raises(ModelRequestError):
        await provider.complete(request())
    assert len(provider._session.bodies) == 3


async def test_text_mode(provider):
    provider._session = Session([(200, completion('主持人回复'))])
    result = await provider.complete(request(output_mode='text'))
    assert result.content == '主持人回复'
    assert 'response_format' not in provider._session.bodies[0]


async def test_explicit_legacy_json_fallback(settings):
    provider = ChatCompletionsProvider(replace(settings, llm_json_output=False))
    provider._session = Session([(200, completion('```json\n{"ok":true}\n```'))])
    assert (await provider.complete(request())).content['ok']
    assert 'response_format' not in provider._session.bodies[0]


@pytest.mark.parametrize('field,value', [('llm_provider', 'unknown'), ('llm_thinking', 'auto'),
                                       ('llm_reasoning_effort', 'bad'), ('llm_timeout', 0)])
def test_invalid_config(settings, field, value):
    with pytest.raises(ValueError):
        validate_settings(replace(settings, **{field: value}))


def test_environment_controls(monkeypatch, tmp_path):
    monkeypatch.setenv('LLM_JSON_OUTPUT', 'false')
    monkeypatch.setenv('LLM_PROVIDER', 'deepseek')
    monkeypatch.setenv('LLM_THINKING', 'enabled')
    monkeypatch.setenv('LLM_REASONING_EFFORT', 'high')
    settings = load_settings(tmp_path / 'missing.env')
    assert not settings.llm_json_output
    assert settings.llm_provider == 'deepseek'
    assert settings.llm_thinking == 'enabled'
    assert settings.llm_reasoning_effort == 'high'
    monkeypatch.setenv('LLM_JSON_OUTPUT', 'maybe')
    with pytest.raises(ValueError):
        load_settings(tmp_path / 'missing.env')


def sse(*chunks, reasoning=None):
    lines = []
    if reasoning:
        lines.append('data: ' + json.dumps({
            'id': 'response-1', 'model': 'test-model',
            'choices': [{'delta': {'reasoning_content': reasoning}, 'finish_reason': None}],
        }))
    for chunk in chunks:
        lines.append('data: ' + json.dumps({
            'id': 'response-1', 'model': 'test-model',
            'choices': [{'delta': {'content': chunk}, 'finish_reason': None}],
        }))
    lines.append('data: ' + json.dumps({
        'id': 'response-1', 'model': 'test-model',
        'choices': [{'delta': {}, 'finish_reason': 'stop'}],
        'usage': {'total_tokens': 42, 'prompt_cache_hit_tokens': 10},
    }))
    lines.append('data: [DONE]')
    return '\n'.join(lines)


async def test_stream_assembles_json_and_forwards_content_deltas(provider):
    provider._session = Session([(200, sse('{"ok":', 'true}'))])
    seen = []
    result = await provider.complete(request(), on_delta=seen.append)
    assert seen == [None, '{"ok":', 'true}']
    assert result.content == {'ok': True}
    assert result.finish_reason == 'stop'
    assert result.usage['total_tokens'] == 42
    assert provider._session.bodies[0]['stream'] is True


async def test_stream_keeps_reasoning_out_of_visible_deltas(provider):
    provider._session = Session([(200, sse('{"ok":true}', reasoning='先想一下'))])
    seen = []
    result = await provider.complete(request(), on_delta=seen.append)
    assert seen == [None, '{"ok":true}']
    assert result.message['reasoning_content'] == '先想一下'


def test_stream_can_be_disabled(settings):
    provider = ChatCompletionsProvider(replace(settings, llm_stream=False))
    assert 'stream' not in provider.build_body(request())


async def test_stream_assembles_native_tool_call(provider):
    first = {'index': 0, 'id': 'call-1', 'type': 'function',
             'function': {'name': 'lookup_public', 'arguments': '{"query":'}}
    second = {'index': 0, 'function': {'arguments': '"码头"}'}}
    chunks = [
        {'delta': {'tool_calls': [first]}, 'finish_reason': None},
        {'delta': {'tool_calls': [second]}, 'finish_reason': None},
        {'delta': {}, 'finish_reason': 'tool_calls'},
    ]
    stream = '\n'.join('data: ' + json.dumps({'choices': [chunk]}) for chunk in chunks)
    provider._session = Session([(200, stream + '\ndata: [DONE]')])
    spec = {'type': 'function', 'function': {'name': 'lookup_public', 'parameters': {'type': 'object'}}}
    response = await provider.complete(request(output_mode='text', tools=[spec]))
    assert provider._session.bodies[0]['tools'] == [spec]
    assert response.message['tool_calls'] == [{
        'id': 'call-1', 'type': 'function',
        'function': {'name': 'lookup_public', 'arguments': '{"query":"码头"}'},
    }]
