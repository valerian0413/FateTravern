"""Retry or cancel interrupted turns without re-rolling persisted dice."""
from __future__ import annotations

from app.domain.errors import ValidationError
from app.domain.types import TurnStage
from app.storage.repositories import TurnRepo


async def retry(runtime, world_id: str, request_id: str):
    turn = await TurnRepo(runtime.db.conn).get_by_request(world_id, request_id)
    if not turn:
        raise ValidationError('请求不存在', path='request_id')
    if turn.stage == TurnStage.COMPLETED:
        return runtime.chat._receipt(world_id, request_id)
    if turn.stage in (TurnStage.CANCELLED, TurnStage.STALE):
        raise ValidationError('请求不可重试', path='request_id')
    return await runtime.chat.retry(turn)


async def cancel(runtime, world_id: str, request_id: str) -> dict:
    async with runtime.db.transaction() as db:
        turns = TurnRepo(db)
        turn = await turns.get_by_request(world_id, request_id)
        if not turn:
            raise ValidationError('请求不存在', path='request_id')
        if turn.stage == TurnStage.COMPLETED:
            raise ValidationError('已完成请求不能取消', path='request_id')
        if turn.stage == TurnStage.COMMITTING:
            raise ValidationError('提交中不能取消', path='stage')
        if (turn.checkpoint.get('combat_decision') or {}).get('kind') == 'encounter_hit':
            source = await turns.get_by_request(world_id, turn.checkpoint['reply_to'])
            if source and source.checkpoint.get('consumed_by') == request_id:
                checkpoint = {k: v for k, v in source.checkpoint.items() if k != 'consumed_by'}
                await turns.checkpoint(world_id, source.id, source.stage, checkpoint, result=source.result)
        await turns.checkpoint(world_id, turn.id, TurnStage.CANCELLED, turn.checkpoint, error_code='cancelled')
    return {'request_id': request_id, 'status': 'cancelled'}
