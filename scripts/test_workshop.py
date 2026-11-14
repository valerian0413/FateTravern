"""Live creation smoke test using invented inputs and an isolated temporary archive.
Run explicitly: .venv/bin/python scripts/test_workshop.py
No existing game archive is read or sent to the provider.
"""
import asyncio
import tempfile
from pathlib import Path
from dataclasses import replace

from app.application import workshop
from app.bootstrap import build_runtime, shutdown
from app.config import load_settings
from app.storage.repositories import WorldRepo


async def main():
    with tempfile.TemporaryDirectory(prefix='trpg-workshop-') as directory:
        rt = await build_runtime(replace(load_settings(),db_path=Path(directory)/'test.sqlite3'))
        await rt.worker.stop()
        try:
            draft=await workshop.create(rt)
            draft=await workshop.message(rt,draft['id'],{'revision':draft['revision'],
                'text':'创建一个小型浮空群岛世界，飞艇往来，人类稀少。初始场景是和平贸易港，货币铜币；超凡是消耗浮晶驱动机械。其余细节请给出一份完整可确认的初稿，未知远方保留留白。'})
            if draft['pending']: raise RuntimeError('unexpected initial world conflict')
            draft=await workshop.confirm(rt,draft['id'],{'revision':draft['revision'],'action':'confirm_world'})
            draft=await workshop.message(rt,draft['id'],{'revision':draft['revision'],
                'text':'角色叫小岚，港口长大的年轻剑士，练习剑术，身体结实，认识一些码头工人。想乘船寻找失踪船长。带普通短剑、旅行衣和小包，三枚铜币。自由形象是熟悉港口人情。请按背景分配一个+2两个+1技能。'})
            if draft['pending']: raise RuntimeError('unexpected character conflict')
            draft=await workshop.confirm(rt,draft['id'],{'revision':draft['revision'],'action':'confirm_character'})
            snapshot=await WorldRepo(rt.db.conn).get_snapshot(draft['world_id'])
            assert snapshot.world.ruleset_id=='fate-lite-v1'
            print('PASS world dialogue → confirmation → character dialogue → isolated archive')
            print('skills:',{k:v for k,v in draft['character']['skills'].items() if v})
            print('stress:',draft['character']['stress_tracks'])
            print('trace:',rt.trace.path)
        finally:
            await shutdown(rt)

if __name__=='__main__':asyncio.run(main())
