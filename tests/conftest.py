"""Shared fixtures: temp DB, fixed world, rules, offline settings."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from app.config import Settings
from app.domain.types import Entity, EntityKind, Phase, World
from app.rules.four_dice import FourDice
from app.storage.database import Database
from app.storage.repositories import EntityRepo, WorldRepo


@pytest.fixture
async def db(tmp_path: Path):
    database = Database(tmp_path / 'test.sqlite3')
    await database.connect()
    await database.migrate()
    yield database
    await database.close()


@pytest.fixture
async def world_snapshot(db):
    wid = '11111111-1111-1111-1111-111111111111'
    pid = '22222222-2222-2222-2222-222222222222'
    place_id = '33333333-3333-3333-3333-333333333333'
    world = World(
        id=wid, name='测试世界', setting='浮空群岛测试', phase=Phase.PLAYING,
        version=0, clock=0, ruleset_id='four-dice-v1',
        ruleset_config=json.loads((Path(__file__).resolve().parents[1] / 'config/rules/four_dice.json').read_text()),
        player_id=pid,
    )
    async with db.transaction():
        await WorldRepo(db.conn).create(world)
        await EntityRepo(db.conn).upsert(Entity(
            id=pid, world_id=wid, kind=EntityKind.PLAYER, name='小剑士',
            attributes={'skills': {'剑术': 2, '自然知识': 1}, 'resources': {'coins': 5}},
            revision=1,
        ))
        await EntityRepo(db.conn).upsert(Entity(
            id=place_id, world_id=wid, kind=EntityKind.PLACE, name='空港',
            attributes={'description': '贸易据点'}, revision=1,
        ))
    snapshot = await WorldRepo(db.conn).get_snapshot(wid)
    return snapshot


@pytest.fixture
def four_dice():
    return FourDice(json.loads(
        (Path(__file__).resolve().parents[1] / 'config/rules/four_dice.json').read_text(encoding='utf-8')
    ))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    root = Path(__file__).resolve().parents[1]
    presets_dir = tmp_path / 'presets'
    shutil.copytree(root / 'config/presets', presets_dir)
    shutil.copy2(root / 'scenarios/presets/sky_islands.json', presets_dir)
    return Settings(
        db_path=tmp_path / 'engine.sqlite3',
        host='127.0.0.1',
        port=8000,
        llm_base_url='https://example.test/v1',
        llm_api_key='test-key',
        llm_model='test-model',
        llm_timeout=30,
        llm_concurrency=2,
        log_dir=tmp_path / 'logs',
        prompt_version='test',
        narration_max_chars=180,
        rules_path=root / 'config/rules/four_dice.json',
        presets_dir=presets_dir,
        prompts_dir=root / 'app/prompts',
    )
