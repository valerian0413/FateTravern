"""JSONL request/response tracing with secret redaction."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
import threading
import uuid


class Trace:
    def __init__(self, path: Path | None = None, log_dir: Path | None = None):
        self.run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
        base = path or (log_dir or Path('logs')) / f'{self.run_id}.jsonl'
        self.path = Path(base)
        self._lock = threading.Lock()

    def _redact(self, line: str) -> str:
        secret = os.getenv('LLM_API_KEY', '')
        if secret:
            line = line.replace(secret, '[REDACTED]')
            line = line.replace(json.dumps(secret, ensure_ascii=False)[1:-1], '[REDACTED]')
        return line

    def write(self, record: dict) -> None:
        record = {'timestamp': datetime.now(timezone.utc).isoformat(), 'run_id': self.run_id, **record}
        line = self._redact(json.dumps(record, ensure_ascii=False))
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, 'a', encoding='utf-8') as file:
                file.write(line + '\n')
                file.flush()

    def record_request(self, meta: dict, body: dict) -> None:
        self.write({**meta, 'event': 'request', 'request': body})

    def record_response(self, meta: dict, raw, parsed, timings: dict, error=None) -> None:
        self.write({
            **meta, 'event': 'response', 'raw_response': raw, 'parsed_output': parsed,
            **timings, 'error': error,
        })
