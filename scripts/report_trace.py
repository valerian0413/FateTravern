#!/usr/bin/env python3
"""Aggregate JSONL trace by call_id — do not sum parallel attempts as turn latency."""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path


def main(path: Path) -> None:
    calls = defaultdict(list)
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        cid = row.get('call_id')
        if cid:
            calls[cid].append(row)
    for cid, rows in calls.items():
        req = next((r for r in rows if r.get('event') == 'request'), None)
        res = next((r for r in rows if r.get('event') == 'response' and not r.get('error')), None)
        print({
            'call_id': cid,
            'module': (req or {}).get('module'),
            'attempts': len([r for r in rows if r.get('event') == 'request']),
            'elapsed_ms': (res or {}).get('elapsed_ms'),
            'usage': (res or {}).get('usage'),
        })


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('trace', type=Path)
    main(parser.parse_args().trace)
