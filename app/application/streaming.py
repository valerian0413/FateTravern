"""Push the growing player-visible text of one model call."""
from __future__ import annotations

from app.api.notify import publish_text


def text_sink(runtime, key: str, channel: str, request_id: str | None):
    async def on_text(text: str) -> None:
        publish_text(runtime, key, channel=channel, request_id=request_id, text=text)

    return on_text
