#!/usr/bin/env python3
"""Minimal live model connectivity check."""
from __future__ import annotations

import argparse
import asyncio
import uuid
from pathlib import Path

from app.config import load_settings
from app.providers.base import ModelRequest
from app.providers.chat_completions import ChatCompletionsProvider
from app.providers.trace import Trace


async def main(env: Path | None) -> int:
    settings = load_settings(env)
    if not all((settings.llm_base_url, settings.llm_api_key, settings.llm_model)):
        print('FAIL: missing LLM_* settings')
        return 1
    provider = ChatCompletionsProvider(settings, Trace(log_dir=settings.log_dir))
    try:
        response = await provider.complete(ModelRequest(
            module='ping', prompt_version='test', schema_version='1', output_budget=256,
            world_id=None, turn_id=None, call_id=uuid.uuid4().hex,
            messages=[
                {'role': 'system', 'content': '只返回 JSON 对象 {"ok": true}'},
                {'role': 'user', 'content': '{"ping":true}'},
            ],
        ))
        if response.content != {'ok': True}:
            print('FAIL: unexpected ping payload', response.content)
            return 1
        print('PASS', response.content)
        print('finish_reason:', response.finish_reason)
        print('timings:', response.timings)
        print('usage:', response.usage)
        return 0
    except Exception as exc:
        print('FAIL', type(exc).__name__, str(exc))
        return 1
    finally:
        print('trace:', provider.trace.path)
        await provider.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--env', type=Path, default=None)
    raise SystemExit(asyncio.run(main(parser.parse_args().env)))
