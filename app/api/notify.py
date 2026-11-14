"""Notify SSE subscribers after persisting stream events."""


def notify_world(runtime, world_id: str) -> None:
    hub = (runtime.streams or {}).get('hub')
    if hub is not None:
        hub.notify(world_id)


def publish_text(runtime, key: str, *, channel: str, request_id: str | None, text: str) -> None:
    hub = (runtime.streams or {}).get('hub')
    if hub is None or not text:
        return
    hub.publish(key, {
        'type': 'assistant.delta',
        'request_id': request_id,
        'ephemeral': True,
        'payload': {'channel': channel, 'text': text, 'request_id': request_id},
    })
