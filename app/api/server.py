"""aiohttp application factory."""
from __future__ import annotations

import json
from pathlib import Path

from aiohttp import web

from app.api import routes
from app.api.streams import StreamHub
from app.bootstrap import Runtime, shutdown
from app.domain.errors import AppError


async def prepare_sse(request):
    response = web.StreamResponse(
        status=200,
        headers={
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
        },
    )
    await response.prepare(request)
    return response


async def write_sse(response, event: dict) -> None:
    if event.get('type') == 'heartbeat':
        await response.write(b': ping\n\n')
        return
    payload = json.dumps(event, ensure_ascii=False)
    prefix = f"id: {event['id']}\n" if event.get('id') is not None else ''
    await response.write(f"{prefix}event: {event['type']}\ndata: {payload}\n\n".encode())


def create_app(runtime: Runtime) -> web.Application:
    app = web.Application()
    hub = StreamHub(runtime)
    runtime.streams['hub'] = hub

    async def on_shutdown(_app):
        await shutdown(runtime)

    app.on_shutdown.append(on_shutdown)

    async def no_cache_frontend(request, response):
        if request.path == '/' or request.path.startswith('/static/'):
            response.headers['Cache-Control'] = 'no-store, max-age=0'

    app.on_response_prepare.append(no_cache_frontend)

    @web.middleware
    async def error_middleware(request, handler):
        try:
            return await handler(request)
        except AppError as exc:
            return web.json_response(exc.to_dict(), status=exc.http_status)
        except KeyError as exc:
            return web.json_response({'code': 'not_found', 'message': str(exc) or '资源不存在', 'retryable': False}, status=404)
        except FileNotFoundError as exc:
            return web.json_response({'code': 'not_found', 'message': str(exc), 'retryable': False}, status=404)
        except Exception:
            return web.json_response(
                {'code': 'service_unavailable', 'message': '服务暂不可用', 'retryable': True},
                status=503,
            )

    app.middlewares.append(error_middleware)

    def bind(method, path, fn):
        async def handler(request):
            status, payload = await fn(runtime, request)
            return web.json_response(payload, status=status)
        app.router.add_route(method, path, handler)

    bind('GET', '/api/v1/workshop', routes.workshop_list)
    bind('POST', '/api/v1/workshop', routes.workshop_create)
    bind('GET', '/api/v1/workshop/catalog', routes.workshop_catalog)
    bind('DELETE', '/api/v1/workshop/catalog/{kind}/{setting_id}', routes.workshop_delete_setting)
    bind('GET', '/api/v1/workshop/{draft_id}', routes.workshop_get)
    bind('POST', '/api/v1/workshop/{draft_id}/messages', routes.workshop_message)
    bind('POST', '/api/v1/workshop/{draft_id}/confirm', routes.workshop_confirm)
    bind('POST', '/api/v1/workshop/{draft_id}/document', routes.workshop_update_document)
    bind('GET', '/api/v1/demos', routes.list_demos)
    bind('POST', '/api/v1/demos/{demo_id}/play', routes.play_demo)
    bind('GET', '/api/v1/worlds', routes.list_worlds)
    bind('POST', '/api/v1/worlds', routes.create_world)
    bind('DELETE', '/api/v1/worlds/{world_id}', routes.delete_world)
    bind('POST', '/api/v1/worlds/{world_id}/start', routes.start_world)
    bind('POST', '/api/v1/worlds/{world_id}/messages', routes.send_message)
    bind('POST', '/api/v1/worlds/{world_id}/proposals/{proposal_id}/confirm', routes.confirm_proposal)
    bind('POST', '/api/v1/worlds/{world_id}/proposals/{proposal_id}/reject', routes.reject_proposal)
    bind('GET', '/api/v1/worlds/{world_id}/requests/{request_id}', routes.get_request)
    bind('POST', '/api/v1/worlds/{world_id}/requests/{request_id}/retry', routes.retry_request)
    bind('POST', '/api/v1/worlds/{world_id}/requests/{request_id}/cancel', routes.cancel_request)
    bind('GET', '/api/v1/worlds/{world_id}/messages', routes.get_messages)
    bind('GET', '/api/v1/worlds/{world_id}/view', routes.get_view)

    async def subscribe_events(request):
        world_id = request.match_info['world_id']
        after = int(request.headers.get('Last-Event-ID') or request.rel_url.query.get('after') or 0)
        response = await prepare_sse(request)
        async for event in hub.subscribe(world_id, after):
            await write_sse(response, event)
        return response

    async def subscribe_workshop(request):
        draft_id = request.match_info['draft_id']
        response = await prepare_sse(request)
        async for event in hub.listen(f'draft:{draft_id}'):
            await write_sse(response, event)
        return response

    app.router.add_get('/api/v1/worlds/{world_id}/events', subscribe_events)
    app.router.add_get('/api/v1/workshop/{draft_id}/events', subscribe_workshop)

    web_root = Path(__file__).resolve().parents[2] / 'web'

    async def index(_request):
        return web.FileResponse(web_root / 'index.html')

    app.router.add_get('/', index)
    app.router.add_static('/static/', web_root, show_index=False)
    return app
