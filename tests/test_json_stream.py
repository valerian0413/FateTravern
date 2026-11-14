"""Partial JSON field extraction and ephemeral text delivery."""
import asyncio

from app.api.streams import StreamHub
from app.providers.json_stream import TextFieldWatch, partial_top_level_string


def test_partial_string_waits_for_the_named_field():
    assert partial_top_level_string('{"other": "x", "reply": "你', 'reply') == '你'
    assert partial_top_level_string('{"reply": "a\\n', 'reply') == 'a\n'
    assert partial_top_level_string('{"document": {"reply": "里面"}, "reply": "外面', 'reply') == '外面'
    assert partial_top_level_string('{"note": "say \\"reply\\"", "reply": "好"', 'reply') == '好'
    assert partial_top_level_string('{"reply"', 'reply') is None


async def test_watcher_resets_between_attempts():
    seen = []

    async def collect(text):
        seen.append(text)

    watch = TextFieldWatch('reply', collect)
    await watch('{"reply": "旧')
    await watch(None)
    await watch('{"reply": "新"}')
    assert seen == ['旧', '新']


async def test_ephemeral_event_reaches_listener_without_a_database():
    hub = StreamHub(None)

    async def once():
        async for event in hub.listen('draft:1'):
            if event['type'] != 'heartbeat':
                return event

    task = asyncio.create_task(once())
    await asyncio.sleep(0)
    hub.publish('draft:1', {'type': 'assistant.delta', 'payload': {'text': '风'}})
    event = await asyncio.wait_for(task, 1)
    assert event['payload']['text'] == '风'
