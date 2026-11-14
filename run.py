#!/usr/bin/env python3
"""Process entry — no business logic."""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from aiohttp import web

from app.api.server import create_app
from app.bootstrap import build_runtime
from app.config import Settings, load_settings


async def main_async(settings: Settings, host: str, port: int) -> None:
    runtime = await build_runtime(settings, require_model=True)
    print(f'World saves: {runtime.db.path}', flush=True)
    print(f'API trace: {runtime.trace.path}', flush=True)
    runner = web.AppRunner(create_app(runtime))
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    print(f'Local backend: http://{host}:{port}', flush=True)
    while True:
        await asyncio.sleep(3600)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description='AI TRPG local backend')
    parser.add_argument('--host', default=None)
    parser.add_argument('--port', type=int, default=None)
    parser.add_argument('--config', type=Path, default=None, help='.env path')
    args = parser.parse_args(argv)
    settings = load_settings(args.config)
    asyncio.run(main_async(settings, args.host or settings.host, args.port or settings.port))


if __name__ == '__main__':
    main()
