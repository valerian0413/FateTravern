#!/usr/bin/env python3
"""Export JSONL model traces into a readable Markdown review, grouped by player input."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sqlite3
from collections import Counter, OrderedDict
from datetime import datetime
from pathlib import Path

from app.config import load_settings


def decode(content):
    if not isinstance(content, str):
        return content
    for parser in (json.loads, ast.literal_eval):
        try:
            return parser(content)
        except (ValueError, SyntaxError, TypeError):
            continue
    return content


def block(value, lang: str = 'json') -> str:
    if isinstance(value, (dict, list)):
        return f'```json\n{json.dumps(value, ensure_ascii=False, indent=1)}\n```'
    return f'```text\n{value}\n```' if lang == 'text' else f'```{lang}\n{value}\n```'


def local_time(ts: str) -> str:
    return datetime.fromisoformat(ts).astimezone().strftime('%m-%d %H:%M:%S')


def load_calls(paths: list[Path], world: str | None) -> list[dict]:
    calls: OrderedDict[str, dict] = OrderedDict()
    for path in paths:
        for line in path.read_text(encoding='utf-8').splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            cid = row.get('call_id')
            if not cid or (world and row.get('world_id') != world):
                continue
            call = calls.setdefault(cid, {'call_id': cid, 'attempts': [], 'file': path.name})
            for key in ('module', 'world_id', 'turn_id', 'prompt_version'):
                call[key] = call.get(key) or row.get(key)
            if row.get('event') == 'request':
                call.setdefault('started', row['timestamp'])
                call['request'] = row.get('request') or {}
                call['attempts'].append({'attempt': row.get('attempt')})
            elif row.get('event') == 'response':
                if not call['attempts']:
                    call['attempts'].append({'attempt': row.get('attempt')})
                call['attempts'][-1].update(row)
    return sorted((c for c in calls.values() if 'started' in c), key=lambda c: c['started'])


def turn_outcomes(db_path: Path) -> dict[str, dict]:
    if not db_path.exists():
        return {}
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    rows = conn.execute('SELECT id, stage, error_code, checkpoint FROM turns').fetchall()
    out = {}
    for tid, stage, error_code, checkpoint in rows:
        error = (decode(checkpoint) or {}).get('error') if checkpoint else None
        out[tid] = {'stage': stage, 'error_code': error_code, 'error': error}
    return out


def render(calls: list[dict], outcomes: dict, sources: list[Path]) -> str:
    prompts: OrderedDict[str, dict] = OrderedDict()
    lines: list[str] = []
    stats = Counter()
    failures = Counter()
    seconds = Counter()

    group = 0
    for call in calls:
        module = call.get('module') or '?'
        messages = (call.get('request') or {}).get('messages') or []
        system = next((m['content'] for m in messages if m['role'] == 'system'), '')
        user = decode(next((m['content'] for m in messages if m['role'] == 'user'), ''))
        digest = hashlib.sha256(system.encode()).hexdigest()[:8]
        prompt_key = f'{module}-{digest}'
        if prompt_key not in prompts:
            prompts[prompt_key] = {'module': module, 'text': system, 'first': call['started'], 'n': len(prompts) + 1}
        prompt_ref = f"提示词 P{prompts[prompt_key]['n']}（{module}）"

        final = call['attempts'][-1] if call['attempts'] else {}
        stats[module] += 1
        seconds[module] += (final.get('elapsed_ms') or 0) / 1000
        if final.get('error') or 'parsed_output' not in final:
            failures[module] += 1

        if group == 0 and module != 'intent' and not lines:
            lines.append('\n---\n\n## 玩家输入之前的调用（建世界、开场等）\n')
        if module == 'intent':
            group += 1
            text = user.get('text') if isinstance(user, dict) else str(user)[:80]
            hint = user.get('mode_hint') if isinstance(user, dict) else ''
            lines.append(f'\n---\n\n## 第 {group} 段 · 玩家输入：{text}\n')
            lines.append(f'时间 {local_time(call["started"])} · mode_hint={hint}')

        tid = call.get('turn_id')
        outcome = outcomes.get(tid or '')
        outcome_text = ''
        if outcome:
            outcome_text = f" · 回合结局 **{outcome['stage']}**"
            if outcome['error_code']:
                outcome_text += f"（{outcome['error_code']}: {outcome['error'] or ''}）"

        lines.append(f'\n### {local_time(call["started"])} · {module}')
        meta = [f"turn `{(tid or '-')[:8]}`{outcome_text}"]
        raw = decode(final.get('raw_response')) if final.get('raw_response') else None
        usage = (raw or {}).get('usage') if isinstance(raw, dict) else None
        meta.append(f"耗时 {final.get('elapsed_ms', '?')} ms")
        if usage:
            reasoning = (usage.get('completion_tokens_details') or {}).get('reasoning_tokens')
            meta.append(f"tokens 入 {usage.get('prompt_tokens')} / 出 {usage.get('completion_tokens')}"
                        + (f"（推理 {reasoning}）" if reasoning else ''))
        if len(call['attempts']) > 1:
            meta.append(f"共尝试 {len(call['attempts'])} 次")
        meta.append(f'system = {prompt_ref}')
        lines.append(' · '.join(meta))

        for attempt in call['attempts']:
            if attempt.get('error'):
                lines.append(f"\n> 第 {attempt.get('attempt')} 次失败：{attempt['error']}")

        lines.append('\n**输入（user）**\n')
        lines.append(block(user, 'text'))
        if 'parsed_output' in final and final['parsed_output'] is not None:
            lines.append('\n**输出（parsed）**\n')
            lines.append(block(final['parsed_output'], 'text'))
        elif final.get('raw_response'):
            lines.append('\n**原始响应（未能解析）**\n')
            lines.append(block(str(final['raw_response'])[:4000], 'text'))
        else:
            lines.append('\n**无响应记录**')
        if isinstance(raw, dict):
            message = ((raw.get('choices') or [{}])[0].get('message') or {})
            if message.get('reasoning_content'):
                lines.append('\n<details><summary>模型推理过程</summary>\n')
                lines.append(block(message['reasoning_content'], 'text'))
                lines.append('\n</details>')

    head = [
        '# 模型调用记录',
        '',
        f"来源：{', '.join(p.name for p in sources)}。共 {len(calls)} 次调用、{group} 段玩家输入。时间为本地时间。",
        '',
        '| 模块 | 调用次数 | 失败 | 累计耗时（秒） |',
        '|---|---|---|---|',
        *[f'| {m} | {n} | {failures[m]} | {seconds[m]:.1f} |' for m, n in stats.most_common()],
        '',
        '提示词全文见文末附录，正文只标注编号。',
    ]
    appendix = ['\n---\n\n# 附录：提示词全文\n']
    for p in prompts.values():
        appendix.append(f"## P{p['n']} · {p['module']}（首次出现 {local_time(p['first'])}）\n")
        appendix.append(block(p['text'], 'text'))
    return '\n'.join(head + lines + appendix) + '\n'


def main() -> None:
    parser = argparse.ArgumentParser(description='把 logs/*.jsonl 导出为可读的 Markdown')
    parser.add_argument('files', nargs='*', type=Path, help='指定日志文件；省略时用最新一个')
    parser.add_argument('--all', action='store_true', help='合并 logs/ 下全部日志')
    parser.add_argument('--world', help='只导出某个 world_id 的调用')
    parser.add_argument('--out', type=Path, help='输出路径，默认 output/traces/<名称>.md')
    args = parser.parse_args()

    settings = load_settings()
    logs = sorted(settings.log_dir.glob('*.jsonl'))
    sources = args.files or (logs if args.all else logs[-1:])
    if not sources:
        raise SystemExit('logs/ 下没有日志文件')
    calls = load_calls(sources, args.world)
    if not calls:
        raise SystemExit('没有符合条件的调用记录')

    name = 'all' if args.all else sources[0].stem if len(sources) == 1 else f'{sources[0].stem}_to_{sources[-1].stem}'
    if args.world:
        name += f'_{args.world[:8]}'
    out = args.out or Path('output/traces') / f'{name}.md'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(calls, turn_outcomes(settings.db_path), sources), encoding='utf-8')
    print(f'{len(calls)} 次调用 → {out}')


if __name__ == '__main__':
    main()
