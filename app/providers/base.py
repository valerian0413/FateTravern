"""Model request/response types."""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ModelRequest:
    module: str
    prompt_version: str
    schema_version: str
    world_id: str | None
    turn_id: str | None
    call_id: str
    messages: list[dict]
    output_budget: int = 1200
    output_mode: str = "json_object"  # json_object or text
    thinking: str | None = None  # enabled/disabled; None inherits settings
    reasoning_effort: str | None = None
    tools: list[dict] = field(default_factory=list)


@dataclass
class ModelResponse:
    content: Any
    raw: str
    usage: dict
    timings: dict = field(default_factory=dict)
    finish_reason: str | None = None
    message: dict = field(default_factory=dict)
    response_id: str | None = None
    model: str | None = None


async def call_model(provider, request: ModelRequest, on_delta=None):
    """Call complete(), passing token callbacks only when the provider accepts them."""
    if on_delta is None:
        return await provider.complete(request)
    params = inspect.signature(provider.complete).parameters
    accepts = 'on_delta' in params or any(item.kind is inspect.Parameter.VAR_KEYWORD for item in params.values())
    if accepts:
        return await provider.complete(request, on_delta=on_delta)
    return await provider.complete(request)
