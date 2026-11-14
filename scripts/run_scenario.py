#!/usr/bin/env python3
"""Run a scenario JSON against a fresh archive and record stats."""
from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.application.creation import prepare, start
from app.bootstrap import build_runtime, shutdown
from app.config import load_settings
from app.storage.repositories import MessageRepo, TurnRepo
from dataclasses import replace


async def main(scenario_path: Path, env: Path | None) -> int:
    settings = load_settings(env)
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    out = Path('test-runs') / run_id
    out.mkdir(parents=True, exist_ok=True)
    db_path = out / 'world.sqlite3'
    scenario_presets = scenario_path.parent / 'presets'
    if scenario_presets.is_dir():
        settings = replace(settings, presets_dir=scenario_presets)
    runtime = await build_runtime(
        replace(settings, db_path=db_path, log_dir=out),
        require_model=True,
    )
    scenario = json.loads(scenario_path.read_text(encoding='utf-8'))
    (out / 'scenario.json').write_text(json.dumps(scenario, ensure_ascii=False, indent=2), encoding='utf-8')
    summary = {'actions': [], 'ok': True}
    try:
        world = await prepare(runtime, scenario.get('preset_id', 'sky-islands-v1'))
        await start(runtime, world.id, 'opening-v1')
        for _ in range(100):
            turn = await TurnRepo(runtime.db.conn).get_by_request(world.id, 'opening-v1')
            if turn and turn.stage.value == 'completed':
                break
            await asyncio.sleep(0.1)
        for action in scenario.get('actions') or []:
            started = datetime.now(timezone.utc)
            await runtime.chat.submit(
                world.id, action['request_id'], action['text'], action.get('mode_hint'),
            )
            for _ in range(300):
                turn = await TurnRepo(runtime.db.conn).get_by_request(world.id, action['request_id'])
                if turn and turn.stage.value in ('completed', 'failed_retryable', 'awaiting_confirmation'):
                    break
                await asyncio.sleep(0.1)
            elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            summary['actions'].append({
                'request_id': action['request_id'],
                'text': action['text'],
                'stage': turn.stage.value if turn else None,
                'elapsed_s': elapsed,
                'result': turn.result if turn else None,
            })
            if not turn or turn.stage.value == 'failed_retryable':
                summary['ok'] = False
        msgs = await MessageRepo(runtime.db.conn).list(world.id)
        summary['messages'] = msgs
        (out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        print('PASS' if summary['ok'] else 'FAIL', out)
        return 0 if summary['ok'] else 1
    finally:
        await shutdown(runtime)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--scenario', type=Path, default=Path('scenarios/sky_port.json'))
    parser.add_argument('--env', type=Path, default=None)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.scenario, args.env)))
