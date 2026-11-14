#!/usr/bin/env python3
"""Read the local save and API traces as a per-request timeline."""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import load_settings


MODULES = {
    'host_tools': '主持人选择工具', 'intent': '识别意图', 'discussion': '咨询回复',
    'generation': '补全世界事实', 'planning': '规划行动', 'audit': '审核变化',
    'cocreation_review': '审核共创', 'narration': '生成叙事', 'memory': '回合摘要',
}
CHANNELS = {'interaction': '与主持人交谈', 'narrative': '旅途见闻'}
EVENTS = {
    'progress': '执行阶段', 'check.announced': '判定告知',
    'interaction.completed': '交谈完成', 'proposal.ready': '共创待确认',
    'turn.committed': '回合已提交', 'request.failed': '请求失败',
}


def short(value, limit=160):
    text = ' '.join(str(value if value is not None else '').split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def lookup_traces(paths, turn_ids):
    by_turn = defaultdict(list)
    for path in paths:
        with path.open(encoding='utf-8') as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue  # A live server may still be appending the last record.
                if isinstance(row, dict) and row.get('turn_id') in turn_ids:
                    by_turn[row['turn_id']].append(row)
    return by_turn


def show_model_calls(rows, details):
    requested = {}
    recorded_tools = {row.get('tool_call_id') for row in rows if row['event'] == 'agent.tool'}
    displayed_results = set()
    for row in sorted(rows, key=lambda row: row['timestamp']):
        event = row['event']
        if event == 'request':
            requested[row['call_id']] = row
            # Older traces contain query results only in the next model request.
            for message in row['request'].get('messages', []):
                tool_id = message.get('tool_call_id')
                if message.get('role') == 'tool' and tool_id not in recorded_tools | displayed_results:
                    displayed_results.add(tool_id)
                    print(f"    工具回传 {tool_id}  {short(message.get('content'), 180)}")
            continue
        time = datetime.fromisoformat(row['timestamp']).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%H:%M:%S')
        if event == 'agent.route':
            print(f"  {time}  CrewAI 分支 → {row['route']}")
        elif event == 'agent.tool':
            print(f"  {time}  Python 工具 {row.get('name') or '参数校验'} · {row['status']}"
                  f"  {short(row.get('result'), 180)}".rstrip())
        elif event == 'response':
            module = row.get('module', '')
            usage = row.get('usage') or {}
            error = row.get('error') or {}
            print(f"  {time}  {MODULES.get(module, module)} · "
                  f"{row.get('elapsed_ms', '—')} ms · 第 {row.get('attempt', '—')} 次 · "
                  f"tokens {usage.get('prompt_tokens', '—')}/{usage.get('completion_tokens', '—')}")
            if error:
                print(f"    错误 {error.get('code')}  {short(error.get('message'))}")
            parsed = row.get('parsed_output')
            if isinstance(parsed, list):
                for call in parsed:
                    if isinstance(call, dict) and isinstance(call.get('function'), dict):
                        function = call['function']
                        print(f"    模型调用 {function.get('name')}  {short(function.get('arguments'), 180)}")
            if details:
                request = requested.get(row['call_id'], {}).get('request', {})
                print(json.dumps({'request': request, 'parsed_output': parsed, 'error': error},
                                 ensure_ascii=False, indent=2))
    if not rows:
        print('  未找到与此回合关联的 API 日志。')


def main():
    parser = argparse.ArgumentParser(description='按玩家请求检查 CrewAI、工具调用和窗口输出')
    parser.add_argument('--world', help='世界名称或世界 ID')
    parser.add_argument('--request', help='请求 ID 或回合 ID，支持前缀')
    parser.add_argument('--limit', type=int, default=1, help='显示最近几条请求，默认 1')
    parser.add_argument('--details', action='store_true', help='展开模型的请求和解析结果')
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error('--limit 必须在 1 到 100 之间')
    settings = load_settings()
    if not settings.db_path.is_file():
        parser.exit(1, '未找到本地存档。\n')
    with closing(sqlite3.connect(settings.db_path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        where, params = [], []
        if args.world:
            where.append('(w.name = ? OR w.id = ?)')
            params.extend([args.world, args.world])
        if args.request:
            where.append('(instr(t.request_id, ?) = 1 OR instr(t.id, ?) = 1)')
            params.extend([args.request, args.request])
        query = '''SELECT t.*, w.name AS world_name FROM turns t
                   JOIN worlds w ON w.id = t.world_id'''
        if where:
            query += ' WHERE ' + ' AND '.join(where)
        turns = list(db.execute(query + ' ORDER BY t.created_at DESC, t.rowid DESC LIMIT ?', [*params, args.limit]))
        traces = lookup_traces(sorted(settings.log_dir.glob('*.jsonl')), {turn['id'] for turn in turns})
        for turn in turns:
            request_id, world_id = turn['request_id'], turn['world_id']
            messages = list(db.execute('SELECT * FROM messages WHERE world_id=? AND turn_id=? ORDER BY sequence',
                                       (world_id, turn['id'])))
            checkpoint = json.loads(turn['checkpoint'] or '{}')
            result = json.loads(turn['result'] or 'null') or {}
            player = next((message['text'] for message in messages if message['author'] == 'player'),
                          checkpoint.get('text', ''))
            print(f"\n{turn['world_name']} · {turn['stage']} · {result.get('kind') or result.get('stop_reason') or '—'}")
            print(f"请求 {request_id} · 回合 {turn['id']}")
            print(f"玩家  {short(player, 300)}")
            print('\n模型与工具')
            show_model_calls(traces.get(turn['id'], []), args.details)
            print('\n应用事件与窗口')
            for event in db.execute('SELECT event_type, payload FROM stream_events WHERE world_id=? AND request_id=? ORDER BY sequence',
                                    (world_id, request_id)):
                kind = event['event_type']
                if kind in EVENTS:
                    payload = json.loads(event['payload'])
                    print(f"  {EVENTS[kind]}  {short(payload.get('stage') or payload.get('error') or payload.get('text'))}".rstrip())
            check = db.execute('SELECT payload FROM checks WHERE world_id=? AND turn_id=?',
                               (world_id, turn['id'])).fetchone()
            if check:
                data = json.loads(check['payload'])
                if data.get('mode') == 'check':
                    print(f"  骰点 {data.get('dice')} · 总值 {data.get('total')} / DC {data.get('dc')}")
            for message in messages:
                print(f"  [{CHANNELS.get(message['channel'], message['channel'])}] "
                      f"{'玩家' if message['author'] == 'player' else '主持人'}  {short(message['text'], 240)}")
            if turn['error_code']:
                print(f"  失败原因 {turn['error_code']}  {short(checkpoint.get('error'))}")
        if not turns:
            print('没有找到匹配的玩家请求。')


if __name__ == '__main__':
    main()
