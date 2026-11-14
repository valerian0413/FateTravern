"""Playable adventure snapshots. Starting one copies a fresh archive."""
from __future__ import annotations

import json
from copy import deepcopy

from app.application.workshop import create_prepared_world, validate_book
from app.domain.errors import NotFound, ValidationError
from app.rules.character import validate_character


def _read(settings) -> list[dict]:
    directory = settings.demos_dir
    if not directory.is_dir():
        return []
    found = []
    seen = set()
    for path in sorted(directory.glob('*.json')):
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or not isinstance(data.get('demo_id'), str) or not data['demo_id']:
            raise ValidationError(f'演示文件无效：{path.name}')
        if data['demo_id'] in seen:
            raise ValidationError(f'演示编号重复：{data["demo_id"]}')
        seen.add(data['demo_id'])
        found.append(data)
    return found


def list_demos(runtime) -> list[dict]:
    rows = []
    for demo in _read(runtime.settings):
        character = demo.get('character') if isinstance(demo.get('character'), dict) else {}
        rows.append({
            'id': demo['demo_id'],
            'name': demo.get('name') or '未命名演示',
            'blurb': demo.get('blurb') or '',
            'character_name': character.get('name') or '',
        })
    return rows


async def play(runtime, demo_id: str):
    demo = next((item for item in _read(runtime.settings) if item['demo_id'] == demo_id), None)
    if demo is None:
        raise NotFound(f'未知演示 {demo_id}')
    book = validate_book(deepcopy(demo.get('book')), complete=True)
    card = validate_character(deepcopy(demo.get('character')))
    revision = demo.get('world_revision', 1)
    if type(revision) is not int or revision < 1:
        raise ValidationError('演示的世界版本无效')
    from app.modules.planning import validate_options
    options = validate_options(demo.get('opening_options', []))
    async with runtime.db.transaction():
        return await create_prepared_world(
            runtime.db.conn, book, card, revision, opening_options=options,
            opening_guide=demo.get('opening_guide'),
        )
