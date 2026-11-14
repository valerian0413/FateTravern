"""Chat Completions adapter: explicit output policy, bounded retries, full tracing."""
from __future__ import annotations

import asyncio
import codecs
import json
import ssl
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from app.config import Settings, validate_settings
from app.domain.errors import ModelRequestError
from app.providers.base import ModelRequest, ModelResponse
from app.providers.trace import Trace


def failure(code: str, message: str, retryable: bool = False) -> ModelRequestError:
    return ModelRequestError(message, code=code, retryable=retryable)


class ChatCompletionsProvider:
    def __init__(self, settings: Settings, trace: Trace | None = None):
        self.settings = settings
        self.trace = trace or Trace(log_dir=settings.log_dir)
        self._sem = asyncio.Semaphore(settings.llm_concurrency)
        self._session: aiohttp.ClientSession | None = None

    async def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def build_body(self, request: ModelRequest) -> dict:
        validate_settings(self.settings)
        if type(request.output_budget) is not int or request.output_budget <= 0:
            raise ValueError('output_budget 必须为正整数')
        if request.output_mode not in {'json_object', 'text'}:
            raise ValueError('output_mode 必须为 json_object/text')
        thinking = request.thinking or self.settings.llm_thinking
        effort = request.reasoning_effort or self.settings.llm_reasoning_effort
        if thinking not in {'enabled', 'disabled'} or effort not in {'low', 'high', 'max'}:
            raise ValueError('无效的思考模式配置')
        messages = [dict(message) for message in request.messages]
        if request.output_mode == 'json_object':
            # Module prompts supply their schema/example. This guard also covers custom callers.
            instruction = '只返回一个合法 JSON 对象，遵循任务指定的字段与样例，不要使用 Markdown 代码围栏。'
            if messages and messages[0].get('role') == 'system' and isinstance(messages[0].get('content'), str):
                messages[0]['content'] += '\n' + instruction
            else:
                messages.insert(0, {'role': 'system', 'content': instruction})
        body = {'model': self.settings.llm_model, 'messages': messages, 'max_tokens': request.output_budget}
        if self.settings.llm_json_output and request.output_mode == 'json_object':
            body['response_format'] = {'type': 'json_object'}
        if request.tools:
            body['tools'] = request.tools
            body['tool_choice'] = 'auto'
        profile = self.settings.llm_provider
        is_deepseek = profile == 'deepseek' or (
            profile == 'auto' and urlsplit(self.settings.llm_base_url).hostname == 'api.deepseek.com'
        )
        if is_deepseek:
            body['thinking'] = {'type': thinking}
            if thinking == 'enabled':
                body['reasoning_effort'] = effort
        if self.settings.llm_stream:
            body['stream'] = True
        return body

    def _parse(self, data: dict, request: ModelRequest, raw: str) -> ModelResponse:
        try:
            choice = data['choices'][0]
            message = choice['message']
            reason = choice['finish_reason']
            if not isinstance(message, dict) or message.get('role') != 'assistant':
                raise ValueError('message')
        except (KeyError, IndexError, TypeError, ValueError):
            raise failure('model_response_invalid', '模型响应缺少有效的 choices/message/finish_reason') from None
        if reason == 'length':
            raise failure('model_output_truncated', '模型输出被截断，请提高该模块 output_budget 或缩短输出')
        if reason == 'content_filter':
            raise failure('model_content_filtered', '模型服务未返回可用内容，请调整输入')
        if reason in {'insufficient_system_resource', 'aborted'}:
            raise failure('model_generation_interrupted', '模型生成中断', True)
        if reason == 'tool_calls' or message.get('tool_calls'):
            if not request.tools:
                raise failure('model_unexpected_tool_call', '当前请求未启用工具调用，模型却返回了工具调用')
            calls = message.get('tool_calls')
            if not isinstance(calls, list) or not calls:
                raise failure('model_tool_call_invalid', '模型未返回有效的工具调用')
            return ModelResponse(
                content=None, raw=raw, usage=data.get('usage') or {}, message=dict(message),
                finish_reason=reason, response_id=data.get('id'), model=data.get('model'),
            )
        if reason != 'stop':
            raise failure('model_finish_reason_invalid', '模型响应没有正常结束')
        content = message.get('content')
        if content is None or (isinstance(content, str) and not content.strip()):
            raise failure('model_empty_output', '模型返回空白内容', True)
        if not isinstance(content, str):
            raise failure('model_response_invalid', '模型 content 必须为字符串')
        parsed = content.strip()
        if request.output_mode == 'json_object':
            # Legacy endpoints can explicitly opt out of response_format.
            if not self.settings.llm_json_output and parsed.startswith('```') and parsed.endswith('```'):
                parsed = '\n'.join(parsed.splitlines()[1:-1])
            try:
                parsed = json.loads(parsed)
            except (ValueError, TypeError):
                raise failure('model_json_invalid', '模型未返回合法 JSON 对象', True) from None
            if not isinstance(parsed, dict):
                raise failure('model_json_invalid', '模型必须返回 JSON 对象，不能返回数组或标量', True)
        return ModelResponse(
            content=parsed, raw=raw, usage=data.get('usage') or {}, message=dict(message),
            finish_reason=reason, response_id=data.get('id'), model=data.get('model'),
        )

    async def _emit(self, on_delta, piece: str | None) -> None:
        if on_delta is None or piece == '':
            return
        result = on_delta(piece)
        if asyncio.iscoroutine(result) or asyncio.isfuture(result):
            await result

    async def _emit_assembled(self, data: dict, on_delta) -> None:
        try:
            content = data['choices'][0]['message'].get('content')
        except (KeyError, IndexError, TypeError):
            return
        if isinstance(content, str) and content:
            await self._emit(on_delta, content)

    async def _read_model_body(self, resp, on_delta):
        if hasattr(resp, 'content'):
            return await self._consume(self._socket_lines(resp), on_delta)
        return await self._consume(self._text_lines(await resp.text()), on_delta)

    async def _text_lines(self, raw: str):
        for line in raw.splitlines():
            yield line

    async def _socket_lines(self, resp):
        pending = ''
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        async for chunk in resp.content.iter_any():
            pending += decoder.decode(chunk)
            while '\n' in pending:
                line, pending = pending.split('\n', 1)
                yield line.rstrip('\r')
        pending += decoder.decode(b'', final=True)
        if pending:
            yield pending.rstrip('\r')

    async def _consume(self, lines, on_delta):
        collected = []
        first = None
        async for line in lines:
            if first is None and not line.strip():
                collected.append(line)
                continue
            first = line
            collected.append(line)
            break
        if first is None:
            raise failure('model_response_invalid', '模型服务返回了空响应')
        if not first.lstrip().startswith('data:'):
            async for line in lines:
                collected.append(line)
            raw = '\n'.join(collected)
            try:
                data = json.loads(raw)
            except ValueError:
                raise failure('model_response_invalid', '模型服务返回了非 JSON 响应') from None
            await self._emit_assembled(data, on_delta)
            return raw, data

        parts: list[str] = []
        reasoning: list[str] = []
        tool_calls: dict[int, dict] = {}
        finish = None
        usage = {}
        response_id = None
        model = None

        async def take(line: str) -> bool:
            nonlocal finish, usage, response_id, model
            stripped = line.strip()
            if not stripped or stripped.startswith(':'):
                return False
            if not stripped.startswith('data:'):
                raise failure('model_response_invalid', '模型流式响应格式无效')
            payload = stripped[5:].strip()
            if payload == '[DONE]':
                return True
            try:
                item = json.loads(payload)
            except ValueError:
                raise failure('model_response_invalid', '模型流式响应不是合法 JSON') from None
            if not isinstance(item, dict):
                raise failure('model_response_invalid', '模型流式响应不是对象')
            if item.get('error'):
                err = item['error']
                message = err.get('message') if isinstance(err, dict) else str(err)
                raise failure('model_stream_error', '模型流式输出失败：' + message, True)
            response_id = item.get('id') or response_id
            model = item.get('model') or model
            if isinstance(item.get('usage'), dict):
                usage = item['usage']
            choices = item.get('choices') or []
            if not choices:
                return False
            choice = choices[0] if isinstance(choices[0], dict) else {}
            if choice.get('finish_reason'):
                finish = choice['finish_reason']
            delta = choice.get('delta') if isinstance(choice.get('delta'), dict) else {}
            # A non-streaming chunk can still arrive with message instead of delta.
            message = choice.get('message') if isinstance(choice.get('message'), dict) else {}
            piece = delta.get('content')
            if piece is None:
                piece = message.get('content')
            if isinstance(piece, str) and piece:
                parts.append(piece)
                await self._emit(on_delta, piece)
            for index, call in enumerate(delta.get('tool_calls') or message.get('tool_calls') or []):
                slot = call.get('index', index) if isinstance(call, dict) else None
                if type(slot) is not int or not 0 <= slot < 32:
                    raise failure('model_tool_call_invalid', '流式工具调用的索引无效')
                entry = tool_calls.setdefault(slot, {'type': 'function', 'function': {'name': '', 'arguments': ''}})
                if call.get('id'):
                    entry['id'] = call['id']
                if call.get('type'):
                    entry['type'] = call['type']
                function = call.get('function') or {}
                if not isinstance(function, dict) or any(
                        not isinstance(function.get(key, ''), str) for key in ('name', 'arguments')):
                    raise failure('model_tool_call_invalid', '流式工具调用参数无效')
                for key in ('name', 'arguments'):
                    entry['function'][key] += function.get(key, '')
            thought = delta.get('reasoning_content') or message.get('reasoning_content')
            if isinstance(thought, str) and thought:
                reasoning.append(thought)
            return False

        if await take(first):
            pass
        else:
            async for line in lines:
                collected.append(line)
                if await take(line):
                    break
        message = {'role': 'assistant', 'content': ''.join(parts)}
        if tool_calls:
            message['tool_calls'] = [tool_calls[index] for index in sorted(tool_calls)]
        if reasoning:
            message['reasoning_content'] = ''.join(reasoning)
        data = {
            'id': response_id, 'model': model, 'usage': usage,
            'choices': [{'finish_reason': finish, 'message': message}],
        }
        return '\n'.join(collected), data

    async def complete(self, request: ModelRequest, on_delta=None) -> ModelResponse:
        body = self.build_body(request)
        endpoint = self.settings.llm_base_url.rstrip('/') + '/chat/completions'
        parts = urlsplit(endpoint)
        meta = {
            'call_id': request.call_id or uuid.uuid4().hex, 'module': request.module,
            'prompt_version': request.prompt_version, 'schema_version': request.schema_version,
            'world_id': request.world_id, 'turn_id': request.turn_id,
            'endpoint': urlunsplit((parts.scheme, parts.hostname or '', parts.path, '', '')),
        }
        overall_start = time.perf_counter()
        format_failures = 0
        for attempt in range(1, 4):
            started = time.perf_counter()
            attempt_meta = {**meta, 'attempt': attempt}
            self.trace.record_request(attempt_meta, body)
            raw = None
            data = None
            response = None
            error = None
            status = None
            network_start = None
            network_end = None
            try:
                async with self._sem:
                    network_start = time.perf_counter()
                    session = await self._session_get()
                    async with session.post(
                        endpoint, json=body,
                        headers={'Authorization': 'Bearer ' + self.settings.llm_api_key, 'Content-Type': 'application/json'},
                        timeout=aiohttp.ClientTimeout(total=self.settings.llm_timeout),
                    ) as resp:
                        status = resp.status
                        if status >= 400:
                            raw = await resp.text()
                            network_end = time.perf_counter()
                            hints = {400: '请求格式或模型名称不被支持', 401: 'API密钥无效或未授权',
                                     402: '账户余额不足', 403: '访问被拒绝', 404: 'API路径或模型不存在',
                                     429: '触发限流或配额不足'}
                            raise failure('model_http_' + str(status), '模型HTTP ' + str(status) + '：' +
                                          hints.get(status, '供应商服务异常'), status in {429, 500, 502, 503, 504})
                        await self._emit(on_delta, None)
                        raw, data = await self._read_model_body(resp, on_delta)
                        network_end = time.perf_counter()
                        response = self._parse(data, request, raw)
            except (aiohttp.ClientError, asyncio.TimeoutError, ssl.SSLError) as exc:
                error = failure('model_network_error', '模型网络连接失败：' + type(exc).__name__, True)
            except ModelRequestError as exc:
                error = exc
            if error and error.code in {'model_empty_output', 'model_json_invalid'}:
                format_failures += 1
            retry = bool(error and error.retryable and attempt < 3 and format_failures < 2)
            now = time.perf_counter()
            timings = {
                'elapsed_ms': round((now - started) * 1000, 2),
                'total_elapsed_ms': round((now - overall_start) * 1000, 2),
                'queue_ms': round(((network_start or now) - started) * 1000, 2),
                'network_ms': round(((network_end or now) - network_start) * 1000, 2) if network_start else None,
                'http_status': status, 'will_retry': retry, 'attempts': attempt,
            }
            choices = data.get('choices') if isinstance(data, dict) else None
            choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
            details = {
                'finish_reason': choice.get('finish_reason'),
                'usage': data.get('usage') if isinstance(data, dict) else None,
            }
            self.trace.record_response(
                attempt_meta | details, raw, (response.message.get('tool_calls') or response.content) if response else None, timings,
                error={'type': type(error).__name__, 'code': error.code, 'message': error.message,
                       'retryable': error.retryable} if error else None,
            )
            if response:
                response.timings = timings
                return response
            if not retry:
                raise error from None
            await asyncio.sleep(2 ** (attempt - 1))
        raise failure('model_unavailable', '模型不可用')
