"""Interaction channel entry: discuss / explore / cocreate / clarify."""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from pydantic import PrivateAttr

from app.application.host_tools import select_host_tool
from app.application.flow import NarrativeFlow, listen, router, start
from app.application.context import build_intent_context, build_world_context
from app.application.encounters import EncounterService, active_encounter, public_encounter
from app.application.turns import ActionCommand, TurnService
from app.domain.errors import Conflict, ValidationError
from app.domain.types import Channel, Intent, IntentKind, Turn, TurnStage
from app.modules import intent as intent_mod
from app.modules import review
from app.application.streaming import text_sink
from app.prompts.loader import load
from app.providers.base import ModelRequest, call_model
from app.providers.json_stream import TextFieldWatch
from app.api.notify import notify_world
from app.storage.repositories import (
    ConflictError,
    EncounterRepo,
    MessageRepo,
    ProposalRepo,
    StreamRepo,
    TurnRepo,
    WorldRepo,
)


@dataclass
class Receipt:
    request_id: str
    status_url: str
    event_url: str

    def to_dict(self) -> dict:
        return {
            'request_id': self.request_id,
            'status_url': self.status_url,
            'event_url': self.event_url,
        }


class ChatService:
    def __init__(self, runtime):
        self.runtime = runtime
        self.turns = TurnService(runtime)
        self.encounters = EncounterService(runtime, self.turns)
        self._tasks: dict[tuple[str, str], asyncio.Task] = {}

    def _receipt(self, world_id: str, request_id: str) -> Receipt:
        return Receipt(
            request_id=request_id,
            status_url=f'/api/v1/worlds/{world_id}/requests/{request_id}',
            event_url=f'/api/v1/worlds/{world_id}/events',
        )

    async def submit(self, world_id: str, request_id: str, text: str, mode_hint: str | None = None,
                     reply_to: str | None = None, *, confirmed_action: bool = False, combat_description: str | None = None) -> Receipt:
        if not isinstance(text, str) or not (0 < len(text.strip()) <= 16000):
            raise ValidationError('需要合法 text', path='text')
        if not isinstance(request_id, str) or not (0 < len(request_id) <= 128):
            raise ValidationError('需要合法 request_id', path='request_id')

        if combat_description is not None and (not isinstance(combat_description, str)
                or not 0 < len(combat_description.strip()) <= 300 or not reply_to):
            raise ValidationError('请提供 1—300 字的退让方式或伤痕描述')
        payload = [text, mode_hint, reply_to]
        if combat_description is not None:
            payload.append(combat_description)
        if confirmed_action:
            payload.append('confirmed_action')
        payload_hash = hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()
        db = self.runtime.db.conn
        turn_id = str(uuid.uuid4())
        turn = Turn(
            id=turn_id,
            request_id=request_id,
            payload_hash=payload_hash,
            world_id=world_id,
            base_version=0,
            stage=TurnStage.RECEIVED,
            checkpoint={'text': text, 'mode_hint': mode_hint, 'reply_to': reply_to,
                        'confirmed_action': confirmed_action},
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        try:
            async with self.runtime.db.transaction():
                turn.base_version = (await WorldRepo(db).get(world_id)).version
                reserved = await TurnRepo(db).reserve(turn)
                if reserved.id == turn_id:
                    pending = await self.runtime.db.fetchall(
                        "SELECT request_id FROM turns WHERE world_id=? AND json_extract(result, '$.base_version')=? "
                        "AND json_extract(result, '$.combat_decision.kind') IS NOT NULL "
                        "AND json_extract(checkpoint, '$.consumed_by') IS NULL "
                        "ORDER BY rowid DESC LIMIT 1",
                        (world_id, turn.base_version),
                    )
                    if pending and reply_to != pending[0]['request_id']:
                        raise Conflict('请先通过主持人给出的选项处理当前攻击')
                    encounter = active_encounter(await EncounterRepo(db).get(world_id))
                    if encounter and encounter.get('hit') and not pending:
                        raise Conflict('受击选项正在处理，请等当前请求结束或重试')
                    if reply_to:
                        source = await TurnRepo(db).get_by_request(world_id, reply_to)
                        if not source or not source.result or text not in source.result.get('options', []):
                            raise Conflict('建议不存在，请自由描述行动或刷新页面')
                        if source.result.get('base_version') != turn.base_version or source.checkpoint.get('consumed_by'):
                            raise Conflict('这个建议已过期或已被选择，请使用最新情境')
                        turn.checkpoint['selected_from'] = {
                            'prompt': source.result.get('prompt', ''), 'options': source.result['options'],
                            'registered': source.checkpoint.get('prepared', {}).get('registered')
                                          if source.result.get('kind') == 'decision' else None,
                        }
                        if combat_description is not None and not source.result.get('combat_decision'):
                            raise ValidationError('此选项不接受战斗描述')
                        if source.result.get('combat_decision'):
                            decision = source.result['combat_decision']
                            turn.checkpoint['combat_decision'] = {
                                **decision, 'choice': source.result['options'].index(text),
                                'description': combat_description,
                            }
                            if decision['kind'] != 'encounter_hit':
                                turn.checkpoint['combat_decision']['prepared'] = source.checkpoint['prepared']
                        await TurnRepo(db).checkpoint(world_id, turn_id, TurnStage.RECEIVED, turn.checkpoint)
                        await TurnRepo(db).checkpoint(world_id, source.id, source.stage,
                            {**source.checkpoint, 'consumed_by': request_id}, result=source.result)
                    await MessageRepo(db).append(
                        world_id, Channel.INTERACTION, 'player', text,
                        request_id=request_id, turn_id=turn_id,
                    )
                    await StreamRepo(db).append(
                        world_id, 'progress', {'stage': 'received'}, request_id=request_id,
                    )
            notify_world(self.runtime, world_id)
        except ConflictError as exc:
            raise Conflict(str(exc), path='request_id') from exc

        if reserved.stage in (TurnStage.COMPLETED, TurnStage.AWAITING_CONFIRMATION):
            return self._receipt(world_id, request_id)
        if reserved.stage in (TurnStage.CANCELLED, TurnStage.STALE):
            raise Conflict('该请求已失效，请重新发送', path='request_id')

        self._schedule(world_id, reserved.id, request_id, text, mode_hint)
        return self._receipt(world_id, request_id)

    def has_active_request(self, world_id: str) -> bool:
        return any(saved_id == world_id and not task.done()
                   for (saved_id, _), task in self._tasks.items())

    def _schedule(self, world_id, turn_id, request_id, text, mode_hint):
        # Background processing survives the HTTP connection.
        key = (world_id, request_id)
        task = self._tasks.get(key)
        if task is None or task.done():
            self._tasks[key] = asyncio.create_task(self._process(world_id, turn_id, request_id, text, mode_hint))
            self._tasks[key].add_done_callback(lambda finished: self._forget(key, finished))

    def _forget(self, key, task):
        if self._tasks.get(key) is task:
            self._tasks.pop(key)

    async def retry(self, turn):
        if turn.stage == TurnStage.AWAITING_CONFIRMATION:
            return self._receipt(turn.world_id, turn.request_id)
        self._schedule(turn.world_id, turn.id, turn.request_id,
                       turn.checkpoint.get('text', ''), turn.checkpoint.get('mode_hint'))
        return self._receipt(turn.world_id, turn.request_id)

    async def close(self):
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _process(self, world_id: str, turn_id: str, request_id: str, text: str, mode_hint: str | None) -> None:
        db = self.runtime.db.conn
        turns = TurnRepo(db)
        try:
            current = await turns.get(world_id, turn_id)
            if current and current.checkpoint.get('encounter_route'):
                await self.encounters.execute(current, current.checkpoint['encounter_route'])
            elif current and isinstance(current.checkpoint.get('intent'), dict):
                await self.turns.resume(turn_id, world_id)
            else:
                flow = InteractionFlow(self, world_id, turn_id, request_id, text, mode_hint)
                await flow.kickoff_async()
        except Exception as exc:
            # Keep the intent recorded by TurnService; retry rebuilds the command from it.
            async with self.runtime.db.transaction():
                current = await turns.get(world_id, turn_id)
                if current and current.stage in (TurnStage.CANCELLED, TurnStage.COMPLETED):
                    return
                await turns.checkpoint(
                    world_id, turn_id, TurnStage.FAILED_RETRYABLE,
                    {**(current.checkpoint if current else {}), 'text': text, 'error': str(exc)},
                    error_code=type(exc).__name__,
                )
                await StreamRepo(db).append(
                    world_id, 'request.failed',
                    {'error': str(exc), 'retryable': True},
                    request_id=request_id,
                )
            notify_world(self.runtime, world_id)

    async def discuss(self, context: dict, question: str) -> dict:
        if self.runtime.provider is None or (self.runtime.fixtures or {}).get('discussion'):
            forced = (self.runtime.fixtures or {}).get('discussion') or {
                'reply': '这属于公开咨询：可以尝试，但不会自动替你执行行动。若要执行，请点击“执行这个行动”。',
                'suggested_action': None,
                'can_execute': False,
            }
            return forced
        spec = load('discussion', self.runtime.settings.prompt_version, self.runtime.settings.prompts_dir)
        response = await call_model(self.runtime.provider, ModelRequest(
            module='discussion',
            prompt_version=self.runtime.settings.prompt_version,
            schema_version='1',
            world_id=context.get('world_id'),
            turn_id=context.get('turn_id'),
            call_id=uuid.uuid4().hex,
            messages=[
                {'role': 'system', 'content': spec.text + '\n只返回 JSON。'},
                {'role': 'user', 'content': json.dumps({'question': question, 'public': context.get('public'), 'history':context.get('history',[])},ensure_ascii=False)},
            ],
        ), on_delta=TextFieldWatch('reply', text_sink(
            self.runtime, context.get('world_id'), 'interaction', context.get('request_id'),
        )))
        result = response.content
        if not isinstance(result,dict) or not isinstance(result.get('reply'),str) or not result['reply'].strip():
            raise ValidationError('咨询回复格式不合法，请重试',path='discussion.reply')
        return result


class InteractionFlow(NarrativeFlow):
    """Route one stored player message to exactly one interaction branch."""

    _chat = PrivateAttr()
    _runtime = PrivateAttr()
    _world_id = PrivateAttr()
    _turn_id = PrivateAttr()
    _request_id = PrivateAttr()
    _text = PrivateAttr()
    _mode_hint = PrivateAttr()
    _context = PrivateAttr()
    _snapshot = PrivateAttr()
    _intent = PrivateAttr()
    _tool_reply = PrivateAttr(default=None)

    def __init__(self, chat, world_id: str, turn_id: str, request_id: str, text: str, mode_hint: str | None):
        super().__init__(tracing=False, suppress_flow_events=True)
        self._chat = chat
        self._runtime = chat.runtime
        self._world_id = world_id
        self._turn_id = turn_id
        self._request_id = request_id
        self._text = text
        self._mode_hint = mode_hint

    @start()
    async def identify_intent(self):
        db = self._runtime.db.conn
        turns = TurnRepo(db)
        world_id, turn_id, request_id = self._world_id, self._turn_id, self._request_id
        text, mode_hint = self._text, self._mode_hint
        current = await turns.get(world_id, turn_id)
        checkpoint = {**(current.checkpoint if current else {}), 'text': text, 'mode_hint': mode_hint}
        async with self._runtime.db.transaction():
            await turns.checkpoint(world_id, turn_id, TurnStage.INTERPRETING, checkpoint)
        snapshot = await WorldRepo(db).get_snapshot(world_id)
        ctx = await build_intent_context(snapshot, text)
        encounter = active_encounter(await EncounterRepo(db).get(world_id))
        if encounter:
            ctx['public']['encounter'] = public_encounter(encounter)
        history = await MessageRepo(db).recent(world_id, channel=None, limit=24)
        ctx['history'] = [{'role':m['author'],'text':m['text']} for m in history[-16:]]
        ctx['turn_id'] = turn_id
        ctx['force_intent'] = (self._runtime.fixtures or {}).get('intent')
        if (checkpoint.get('combat_decision') or checkpoint.get('confirmed_action')
                or (checkpoint.get('selected_from') and (encounter or self._runtime.provider is None
                    or ctx['force_intent']))):
            recognized = Intent(kind=IntentKind.ENCOUNTER if encounter else IntentKind.EXPLORE,
                                normalized_action=text, target_ids=[],
                                creation_statements=[], needs_clarification=False, requested_tools=[])
        else:
            if self._runtime.provider is None or ctx['force_intent']:
                recognized = await intent_mod.recognize(
                    self._runtime.provider, self._runtime.settings, ctx, text, mode_hint,
                )
            else:
                recognized, self._tool_reply = await select_host_tool(self._runtime, ctx, text, mode_hint)
            # Free input can also continue a displayed decision. Keep its candidate facts stable.
            if (recognized.kind == IntentKind.EXPLORE and not recognized.needs_clarification
                    and not checkpoint.get('selected_from')):
                for message in reversed(history):
                    if message['author'] != 'gm' or not message.get('request_id'):
                        continue
                    source = await turns.get_by_request(world_id, message['request_id'])
                    result = (source.result or {}) if source else {}
                    if (result.get('kind') != 'decision' or result.get('combat_decision')
                            or result.get('base_version') != snapshot.world.version
                            or source.checkpoint.get('consumed_by')):
                        continue
                    checkpoint['selected_from'] = {
                        'prompt': result['prompt'], 'options': result['options'],
                        'registered': source.checkpoint.get('prepared', {}).get('registered'),
                    }
                    async with self._runtime.db.transaction():
                        await turns.checkpoint(world_id, turn_id, TurnStage.INTERPRETING, checkpoint)
                        await turns.checkpoint(world_id, source.id, source.stage,
                            {**source.checkpoint, 'consumed_by': request_id}, result=result)
                    break
        self._context = ctx
        self._snapshot = snapshot
        self._intent = recognized
        if encounter and recognized.kind == IntentKind.EXPLORE and not recognized.needs_clarification:
            recognized.kind = IntentKind.ENCOUNTER
        if encounter and (checkpoint.get('confirmed_action') or recognized.kind == IntentKind.COCREATE):
            self._intent = Intent(kind=IntentKind.CLARIFY, target_ids=[], creation_statements=[],
                                  needs_clarification=True, requested_tools=[],
                                  question='请先处理当前遭遇，再确认设定提案。')
        self._runtime.trace.write({
            'event': 'agent.route', 'turn_id': turn_id, 'world_id': world_id,
            'request_id': request_id, 'route': 'clarify' if self._intent.needs_clarification else self._intent.kind.value,
        })

    @router(identify_intent, emit=['clarify', 'discussion', 'cocreate', 'explore', 'encounter'])
    def route_intent(self):
        if self._intent.needs_clarification:
            return 'clarify'
        return self._intent.kind.value

    @listen('clarify')
    async def ask_for_clarification(self):
        question = self._intent.question or '请说明你想怎样继续？'
        await self.complete_interaction(question, {'kind': 'clarify', 'question': question})

    @listen('discussion')
    async def answer_question(self):
        async with self._runtime.db.transaction() as db:
            await TurnRepo(db).checkpoint(
                self._world_id, self._turn_id, TurnStage.DISCUSSING,
                {'text': self._text, 'mode_hint': self._mode_hint},
            )
        self._context['request_id'] = self._request_id
        reply = self._tool_reply
        if reply is None:
            reply = await self._chat.discuss(self._context, self._text)
        await self.complete_interaction(reply['reply'], {**reply, 'kind': 'discussion'})

    async def complete_interaction(self, text, result):
        db = self._runtime.db.conn
        async with self._runtime.db.transaction():
            message = await MessageRepo(db).append(
                self._world_id, Channel.INTERACTION, 'gm', text,
                request_id=self._request_id, turn_id=self._turn_id,
            )
            await StreamRepo(db).append(
                self._world_id, 'interaction.completed',
                {'message_id': message['id'], 'text': text, 'suggested_action': result.get('suggested_action')},
                request_id=self._request_id,
            )
            await TurnRepo(db).checkpoint(
                self._world_id, self._turn_id, TurnStage.COMPLETED, {'intent': result['kind']},
                result={**result, 'messages': [message]},
            )
        notify_world(self._runtime, self._world_id)

    @listen('cocreate')
    async def review_proposal(self):
        db = self._runtime.db.conn
        turns = TurnRepo(db)
        world_id, turn_id, request_id = self._world_id, self._turn_id, self._request_id
        text, snapshot = self._text, self._snapshot
        await turns.checkpoint(world_id, turn_id, TurnStage.INTERPRETING, {'text': text, 'mode_hint': self._mode_hint})
        world_ctx = await build_world_context(snapshot, list(snapshot.entities)[:20])
        world_ctx['turn_id'] = turn_id
        world_ctx['force_review'] = (self._runtime.fixtures or {}).get('review')
        proposal = await review.review_creation(
            self._runtime.provider, self._runtime.settings, text, world_ctx,
            snapshot.world.version, world_id,
        )
        # If blocking issues, keep pending with suggested text.
        async with self._runtime.db.transaction():
            await ProposalRepo(db).save(proposal)
            note = proposal.proposed_text
            if proposal.issues:
                note = f"共创需要确认：{proposal.proposed_text}"
            msg = await MessageRepo(db).append(
                world_id, Channel.INTERACTION, 'gm', note,
                request_id=request_id, turn_id=turn_id,
            )
            await StreamRepo(db).append(
                world_id, 'proposal.ready',
                {
                    'proposal_id': proposal.id, 'revision': proposal.revision,
                    'base_version': proposal.base_version,
                    'original_text': proposal.original_text,
                    'proposed_text': proposal.proposed_text,
                    'issues': proposal.issues,
                    'message_id': msg['id'],
                },
                request_id=request_id,
            )
            await turns.checkpoint(
                world_id, turn_id, TurnStage.AWAITING_CONFIRMATION,
                {'proposal_id': proposal.id},
                result={'kind': 'proposal', 'proposal_id': proposal.id, 'messages': [msg]},
            )
        notify_world(self._runtime, world_id)

    @listen('explore')
    async def play_action(self):
        await self._chat.turns.execute(ActionCommand(
            world_id=self._world_id,
            request_id=self._request_id,
            turn_id=self._turn_id,
            text=self._text,
            intent=self._intent,
            base_version=self._snapshot.world.version,
        ))

    @listen('encounter')
    async def play_encounter(self):
        turn = await TurnRepo(self._runtime.db.conn).get(self._world_id, self._turn_id)
        public = self._context['public']
        route = {
            'kind': self._intent.encounter_kind, 'action': self._intent.normalized_action or self._text,
            'target_ids': self._intent.target_ids,
            'context': {**public, 'history': self._context['history'][-8:]},
        }
        await self._chat.encounters.execute(turn, route)
