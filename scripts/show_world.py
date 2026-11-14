#!/usr/bin/env python3
"""Print a world's stored lore (entities, facts, knowledge, relations, NPC cards, schedule, summaries) — read-only."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from app.config import load_settings


def _json(text: str | None):
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _fmt(value) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return '' if value is None else str(value)


def section(title: str, rows: list) -> None:
    print(f'\n## {title}（{len(rows)}）')
    if not rows:
        print('  （空）')


def main(db_path: Path, world_id: str | None, show_events: bool) -> None:
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    q = lambda sql, *args: conn.execute(sql, args).fetchall()

    worlds = q('SELECT * FROM worlds ORDER BY version DESC')
    if not worlds:
        print('数据库里没有世界。')
        return
    if world_id is None:
        print('# 所有世界')
        for w in worlds:
            print(f"  {w['id']}  {w['name']}  phase={w['phase']} version={w['version']} clock={w['clock']}")
        world_id = worlds[0]['id']
        print(f'\n未指定 --world，默认显示 version 最高的：{world_id}')
    world = next((w for w in worlds if w['id'] == world_id), None)
    if world is None:
        print(f'找不到世界 {world_id}')
        return

    entities = q('SELECT * FROM entities WHERE world_id=? ORDER BY kind, name', world_id)
    facts = q('SELECT * FROM facts WHERE world_id=? ORDER BY subject_id, predicate', world_id)
    names = {e['id']: f"{e['name']}<{e['kind']}>" for e in entities}
    fact_text = {f['id']: f"{names.get(f['subject_id'], f['subject_id'])}.{f['predicate']}" for f in facts}
    name = lambda eid: names.get(eid, eid)

    print(f"\n# 世界：{world['name']}  ({world['id']})")
    print(f"  阶段 {world['phase']} · 版本 {world['version']} · 游戏时钟 {world['clock']} 分钟 · 玩家 {name(world['player_id'])}")
    print(f"  设定：{world['setting']}")

    section('实体 entities', entities)
    for e in entities:
        loc = f" @ {name(e['location_id'])}" if e['location_id'] else ''
        print(f"  [{e['kind']}] {e['name']}{loc}  rev={e['revision']}  id={e['id']}")
        for key, value in (_json(e['attributes']) or {}).items():
            print(f'      {key}: {_fmt(value)}')

    section('事实 facts', facts)
    for f in facts:
        rule = _json(f['disclosure_rule']) or {}
        disclosure = rule.get('disclosure', '(无规则)')
        extra = {k: v for k, v in rule.items() if k != 'disclosure'}
        status = '' if f['status'] == 'active' else f"  [{f['status']}]"
        print(f"  {name(f['subject_id'])} · {f['predicate']} = {_fmt(_json(f['value']))}{status}")
        print(f"      披露={disclosure}{'  条件=' + _fmt(extra) if extra else ''}  id={f['id']}")

    knowledge = q('SELECT * FROM knowledge WHERE world_id=? ORDER BY observer_id, learned_at', world_id)
    section('知识 knowledge（谁知道什么）', knowledge)
    for k in knowledge:
        what = fact_text.get(k['fact_id'], k['fact_id']) if k['fact_id'] else k['belief_text']
        print(f"  {name(k['observer_id'])} 知道 {what}  basis={k['basis']} 时钟={k['learned_at']}")

    relations = q('SELECT * FROM relations WHERE world_id=?', world_id)
    section('关系 relations', relations)
    for r in relations:
        print(f"  {name(r['source_entity_id'])} -[{r['kind']}]-> {name(r['target_entity_id'])}：{r['description']}")

    cards = q('SELECT * FROM npc_cards WHERE world_id=?', world_id)
    section('NPC 卡 npc_cards', cards)
    for c in cards:
        print(f"  {name(c['entity_id'])}：{c['identity']}")
        for key in ('traits', 'goals', 'boundaries'):
            print(f'      {key}: {_fmt(_json(c[key]))}')
        print(f"      当前：{c['current_activity']} @ {name(c['location_id'])} · 状态 {c['condition']}")

    scheduled = q('SELECT * FROM scheduled_events WHERE world_id=? ORDER BY due_at', world_id)
    section('计划事件 scheduled_events', scheduled)
    for s in scheduled:
        actors = ', '.join(name(a) for a in _json(s['actor_ids']) or [])
        print(f"  时钟 {s['due_at']} [{s['status']}] {actors}：{s['intent']}")

    summaries = q('SELECT rowid, * FROM summaries WHERE world_id=? ORDER BY rowid', world_id)
    section('摘要 summaries', summaries)
    for s in summaries:
        ents = ', '.join(name(e) for e in _json(s['entity_ids']) or [])
        print(f"  #{s['rowid']} 受众={name(s['audience_id'])} 涉及=[{ents}]")
        print(f"      {s['text']}")

    if show_events:
        events = q('SELECT * FROM events WHERE world_id=? ORDER BY game_time, rowid', world_id)
        section('事件 events（每轮提交的变更）', events)
        for ev in events:
            ops = [c.get('op') for c in _json(ev['changes']) or []]
            print(f"  时钟 {ev['game_time']} {ev['kind']} ops={ops}")
            print(f"      {ev['player_visible_summary']}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='只读查看世界书内容')
    parser.add_argument('--world', help='world_id；省略时列出所有世界并显示 version 最高的')
    parser.add_argument('--db', type=Path, default=None, help='默认读取 .env 中的 DB_PATH')
    parser.add_argument('--events', action='store_true', help='同时列出每轮提交的事件')
    args = parser.parse_args()
    main(args.db or load_settings().db_path, args.world, args.events)
