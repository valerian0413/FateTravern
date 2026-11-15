"""Repositories. Domain writes go through CommitService; messages/checkpoints may write alone."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import aiosqlite

from app.storage.dbutil import fetchall
from app.domain.inventory import unpack_legacy_coins
from app.domain.types import (
    Channel,
    Entity,
    EntityKind,
    Fact,
    FactStatus,
    Knowledge,
    KnowledgeBasis,
    NpcCard,
    Phase,
    Proposal,
    ProposalStatus,
    Relation,
    ScheduledEvent,
    Snapshot,
    Turn,
    TurnStage,
    World,
)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _load(value: str | None, default=None):
    if value is None:
        return default
    return json.loads(value)


class WorldRepo:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def list(self) -> list[dict]:
        rows = await fetchall(self.db, 
            'SELECT id, name, phase, version, clock FROM worlds ORDER BY rowid DESC'
        )
        return [dict(r) for r in rows]

    async def create(self, world: World) -> World:
        await self.db.execute(
            'INSERT INTO worlds(id,name,setting,phase,version,clock,ruleset_id,ruleset_config,player_id) '
            'VALUES (?,?,?,?,?,?,?,?,?)',
            (
                world.id, world.name, world.setting, world.phase.value, world.version,
                world.clock, world.ruleset_id, _json(world.ruleset_config), world.player_id,
            ),
        )
        return world

    async def get(self, world_id: str) -> World:
        row = await fetchall(self.db, 'SELECT * FROM worlds WHERE id=?', (world_id,))
        if not row:
            raise KeyError('世界不存在')
        r = row[0]
        return World(
            id=r['id'], name=r['name'], setting=r['setting'], phase=Phase(r['phase']),
            version=r['version'], clock=r['clock'], ruleset_id=r['ruleset_id'],
            ruleset_config=_load(r['ruleset_config'], {}), player_id=r['player_id'],
        )

    async def get_snapshot(self, world_id: str) -> Snapshot:
        world = await self.get(world_id)
        entities = {
            e.id: e for e in await EntityRepo(self.db).by_world(world_id)
        }
        unpack_legacy_coins(world, entities)
        facts = {
            f.id: f for f in await FactRepo(self.db).by_world(world_id)
        }
        knowledge = {
            k.id: k for k in await KnowledgeRepo(self.db).by_world(world_id)
        }
        relations = {
            r.id: r for r in await RelationRepo(self.db).by_world(world_id)
        }
        npc_cards = {
            c.entity_id: c for c in await NpcCardRepo(self.db).by_world(world_id)
        }
        scheduled = {
            s.id: s for s in await ScheduledRepo(self.db).by_world(world_id)
        }
        return Snapshot(
            world=world, entities=entities, facts=facts, knowledge=knowledge,
            relations=relations, npc_cards=npc_cards, scheduled=scheduled,
        )

    async def bump_version(self, world_id: str, expected: int, clock: int | None = None) -> None:
        if clock is None:
            sql = 'UPDATE worlds SET version=version+1 WHERE id=? AND version=?'
            args = (world_id, expected)
        else:
            sql = 'UPDATE worlds SET version=version+1, clock=? WHERE id=? AND version=?'
            args = (clock, world_id, expected)
        cursor = await self.db.execute(sql, args)
        if cursor.rowcount != 1:
            raise RuntimeError('世界版本冲突')

    async def set_phase(self, world_id: str, phase: Phase) -> None:
        await self.db.execute('UPDATE worlds SET phase=? WHERE id=?', (phase.value, world_id))


class EntityRepo:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def by_world(self, world_id: str) -> list[Entity]:
        rows = await fetchall(self.db, 'SELECT * FROM entities WHERE world_id=?', (world_id,))
        return [self._row(r) for r in rows]

    async def upsert(self, entity: Entity) -> None:
        await self.db.execute(
            'INSERT INTO entities(world_id,id,kind,name,attributes,revision,source_event_id,location_id) '
            'VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(world_id,id) DO UPDATE SET '
            'kind=excluded.kind,name=excluded.name,attributes=excluded.attributes,'
            'revision=excluded.revision,source_event_id=excluded.source_event_id,location_id=excluded.location_id',
            (
                entity.world_id, entity.id, entity.kind.value, entity.name, _json(entity.attributes),
                entity.revision, entity.source_event_id, entity.attributes.get('location_id'),
            ),
        )

    def _row(self, r) -> Entity:
        return Entity(
            id=r['id'], world_id=r['world_id'], kind=EntityKind(r['kind']), name=r['name'],
            attributes=_load(r['attributes'], {}), revision=r['revision'],
            source_event_id=r['source_event_id'],
        )


class FactRepo:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def by_world(self, world_id: str) -> list[Fact]:
        rows = await fetchall(self.db, 'SELECT * FROM facts WHERE world_id=?', (world_id,))
        return [self._row(r) for r in rows]

    async def upsert(self, fact: Fact) -> None:
        await self.db.execute(
            'INSERT INTO facts(world_id,id,subject_id,predicate,value,status,source_event_id,supersedes_id,disclosure_rule) '
            'VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(world_id,id) DO UPDATE SET '
            'status=excluded.status,value=excluded.value,supersedes_id=excluded.supersedes_id,'
            'disclosure_rule=excluded.disclosure_rule,source_event_id=excluded.source_event_id',
            (
                fact.world_id, fact.id, fact.subject_id, fact.predicate, _json(fact.value),
                fact.status.value, fact.source_event_id, fact.supersedes_id, _json(fact.disclosure_rule),
            ),
        )

    def _row(self, r) -> Fact:
        return Fact(
            id=r['id'], world_id=r['world_id'], subject_id=r['subject_id'], predicate=r['predicate'],
            value=_load(r['value']), status=FactStatus(r['status']), source_event_id=r['source_event_id'],
            supersedes_id=r['supersedes_id'], disclosure_rule=_load(r['disclosure_rule']),
        )


class KnowledgeRepo:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def by_world(self, world_id: str) -> list[Knowledge]:
        rows = await fetchall(self.db, 'SELECT * FROM knowledge WHERE world_id=?', (world_id,))
        return [self._row(r) for r in rows]

    async def insert(self, item: Knowledge) -> None:
        await self.db.execute(
            'INSERT INTO knowledge(world_id,id,observer_id,fact_id,belief_text,basis,source_event_id,learned_at) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (
                item.world_id, item.id, item.observer_id, item.fact_id, item.belief_text,
                item.basis.value, item.source_event_id, item.learned_at,
            ),
        )

    def _row(self, r) -> Knowledge:
        return Knowledge(
            id=r['id'], world_id=r['world_id'], observer_id=r['observer_id'], fact_id=r['fact_id'],
            belief_text=r['belief_text'], basis=KnowledgeBasis(r['basis']),
            source_event_id=r['source_event_id'], learned_at=r['learned_at'],
        )


class RelationRepo:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def by_world(self, world_id: str) -> list[Relation]:
        rows = await fetchall(self.db, 'SELECT * FROM relations WHERE world_id=?', (world_id,))
        return [
            Relation(
                id=r['id'], source_entity_id=r['source_entity_id'], target_entity_id=r['target_entity_id'],
                kind=r['kind'], description=r['description'],
                evidence_event_ids=_load(r['evidence_event_ids'], []),
            )
            for r in rows
        ]

    async def insert(self, rel: Relation, world_id: str) -> None:
        await self.db.execute(
            'INSERT INTO relations(world_id,id,source_entity_id,target_entity_id,kind,description,evidence_event_ids) '
            'VALUES (?,?,?,?,?,?,?)',
            (
                world_id, rel.id, rel.source_entity_id, rel.target_entity_id, rel.kind,
                rel.description, _json(rel.evidence_event_ids),
            ),
        )


class NpcCardRepo:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def by_world(self, world_id: str) -> list[NpcCard]:
        rows = await fetchall(self.db, 'SELECT * FROM npc_cards WHERE world_id=?', (world_id,))
        return [
            NpcCard(
                entity_id=r['entity_id'], identity=r['identity'], traits=_load(r['traits'], []),
                goals=_load(r['goals'], []), boundaries=_load(r['boundaries'], []),
                current_activity=r['current_activity'], location_id=r['location_id'],
                condition=r['condition'],
            )
            for r in rows
        ]

    async def upsert(self, world_id: str, card: NpcCard) -> None:
        await self.db.execute(
            'INSERT INTO npc_cards(world_id,entity_id,identity,traits,goals,boundaries,current_activity,location_id,condition) '
            'VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(world_id,entity_id) DO UPDATE SET '
            'identity=excluded.identity,traits=excluded.traits,goals=excluded.goals,'
            'boundaries=excluded.boundaries,current_activity=excluded.current_activity,'
            'location_id=excluded.location_id,condition=excluded.condition',
            (
                world_id, card.entity_id, card.identity, _json(card.traits), _json(card.goals),
                _json(card.boundaries), card.current_activity, card.location_id, card.condition,
            ),
        )


class ScheduledRepo:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def by_world(self, world_id: str) -> list[ScheduledEvent]:
        rows = await fetchall(self.db, 
            'SELECT * FROM scheduled_events WHERE world_id=?', (world_id,)
        )
        return [
            ScheduledEvent(
                id=r['id'], world_id=r['world_id'], due_at=r['due_at'],
                prerequisites=_load(r['prerequisites'], []), actor_ids=_load(r['actor_ids'], []),
                intent=r['intent'], status=r['status'], source_event_id=r['source_event_id'],
            )
            for r in rows
        ]

    async def upsert(self, event: ScheduledEvent) -> None:
        await self.db.execute(
            'INSERT INTO scheduled_events(world_id,id,due_at,prerequisites,actor_ids,intent,status,source_event_id) '
            'VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(world_id,id) DO UPDATE SET '
            'status=excluded.status,due_at=excluded.due_at',
            (
                event.world_id, event.id, event.due_at, _json(event.prerequisites),
                _json(event.actor_ids), event.intent, event.status, event.source_event_id,
            ),
        )


class TurnRepo:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def get_by_request(self, world_id: str, request_id: str) -> Turn | None:
        rows = await fetchall(self.db, 
            'SELECT * FROM turns WHERE world_id=? AND request_id=?', (world_id, request_id)
        )
        return self._row(rows[0]) if rows else None

    async def get(self, world_id: str, turn_id: str) -> Turn | None:
        rows = await fetchall(self.db, 
            'SELECT * FROM turns WHERE world_id=? AND id=?', (world_id, turn_id)
        )
        return self._row(rows[0]) if rows else None

    async def reserve(self, turn: Turn) -> Turn:
        existing = await self.get_by_request(turn.world_id, turn.request_id)
        if existing:
            if existing.payload_hash != turn.payload_hash:
                raise ConflictError('同一 request_id 不能绑定不同正文')
            return existing
        await self.db.execute(
            'INSERT INTO turns(world_id,id,request_id,payload_hash,base_version,stage,checkpoint,result,error_code,created_at) '
            'VALUES (?,?,?,?,?,?,?,?,?,?)',
            (
                turn.world_id, turn.id, turn.request_id, turn.payload_hash, turn.base_version,
                turn.stage.value, _json(turn.checkpoint), _json(turn.result), turn.error_code,
                turn.created_at or datetime.now(timezone.utc).isoformat(),
            ),
        )
        return turn

    async def checkpoint(self, world_id: str, turn_id: str, stage: TurnStage, checkpoint: dict,
                         result: dict | None = None, error_code: str | None = None) -> None:
        await self.db.execute(
            'UPDATE turns SET stage=?, checkpoint=?, result=?, error_code=? WHERE world_id=? AND id=?',
            (stage.value, _json(checkpoint), _json(result), error_code, world_id, turn_id),
        )

    def _row(self, r) -> Turn:
        return Turn(
            id=r['id'], request_id=r['request_id'], payload_hash=r['payload_hash'],
            world_id=r['world_id'], base_version=r['base_version'], stage=TurnStage(r['stage']),
            checkpoint=_load(r['checkpoint'], {}), result=_load(r['result']),
            error_code=r['error_code'], created_at=r['created_at'],
        )


class ConflictError(RuntimeError):
    pass


class ProposalRepo:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def save(self, proposal: Proposal) -> None:
        await self.db.execute(
            'INSERT INTO proposals(world_id,id,revision,base_version,original_text,proposed_text,fact_candidates,issues,status) '
            'VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(world_id,id) DO UPDATE SET '
            'revision=excluded.revision,proposed_text=excluded.proposed_text,fact_candidates=excluded.fact_candidates,'
            'issues=excluded.issues,status=excluded.status,base_version=excluded.base_version',
            (
                proposal.world_id, proposal.id, proposal.revision, proposal.base_version,
                proposal.original_text, proposal.proposed_text, _json(proposal.fact_candidates),
                _json(proposal.issues), proposal.status.value,
            ),
        )

    async def get(self, world_id: str, proposal_id: str) -> Proposal | None:
        rows = await fetchall(self.db, 
            'SELECT * FROM proposals WHERE world_id=? AND id=?', (world_id, proposal_id)
        )
        if not rows:
            return None
        r = rows[0]
        return Proposal(
            id=r['id'], revision=r['revision'], world_id=r['world_id'], base_version=r['base_version'],
            original_text=r['original_text'], proposed_text=r['proposed_text'],
            fact_candidates=_load(r['fact_candidates'], []), issues=_load(r['issues'], []),
            status=ProposalStatus(r['status']),
        )

    async def update(self, proposal: Proposal) -> None:
        await self.save(proposal)


class MessageRepo:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def next_sequence(self, world_id: str) -> int:
        rows = await fetchall(self.db, 
            'SELECT COALESCE(MAX(sequence), 0) FROM messages WHERE world_id=?', (world_id,)
        )
        return int(rows[0][0]) + 1

    async def append(self, world_id: str, channel: Channel, author: str, text: str,
                     request_id: str | None = None, turn_id: str | None = None) -> dict:
        seq = await self.next_sequence(world_id)
        mid = str(uuid.uuid4())
        await self.db.execute(
            'INSERT INTO messages(world_id,id,channel,author,text,sequence,request_id,turn_id) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (world_id, mid, channel.value, author, text, seq, request_id, turn_id),
        )
        return {
            'id': mid, 'world_id': world_id, 'channel': channel.value, 'author': author,
            'text': text, 'sequence': seq, 'request_id': request_id, 'turn_id': turn_id,
        }

    async def list(self, world_id: str, channel: str | None = None, after: int = 0, limit: int = 200) -> list[dict]:
        if channel:
            rows = await fetchall(self.db, 
                'SELECT * FROM messages WHERE world_id=? AND channel=? AND sequence>? ORDER BY sequence LIMIT ?',
                (world_id, channel, after, limit),
            )
        else:
            rows = await fetchall(self.db, 
                'SELECT * FROM messages WHERE world_id=? AND sequence>? ORDER BY sequence LIMIT ?',
                (world_id, after, limit),
            )
        messages = [dict(r) for r in rows]
        for msg in messages:
            if msg['channel'] != 'interaction' or msg['author'] != 'gm' or not msg.get('request_id'):
                continue
            turn = await TurnRepo(self.db).get_by_request(world_id, msg['request_id'])
            result = turn.result if turn and turn.result else {}
            last = (result.get('messages') or [{}])[-1]
            if last.get('id') == msg['id'] and result.get('options'):
                msg['options'] = result['options']
                msg['choice_request_id'] = turn.request_id
                combat = result.get('combat_decision')
                if combat:
                    msg['combat_choice_kind'] = combat['kind']
                snapshot_version = await fetchall(self.db, 'SELECT version FROM worlds WHERE id=?', (world_id,))
                msg['choices_active'] = bool(snapshot_version and snapshot_version[0][0] == result.get('base_version')
                                             and not turn.checkpoint.get('consumed_by'))
        return messages

    async def recent(self, world_id: str, channel: str = 'interaction', limit: int = 16) -> list[dict]:
        if channel is None:
            rows = await fetchall(self.db, 'SELECT * FROM messages WHERE world_id=? ORDER BY sequence DESC LIMIT ?', (world_id,limit))
        else:
            rows = await fetchall(self.db,
                'SELECT * FROM messages WHERE world_id=? AND channel=? ORDER BY sequence DESC LIMIT ?',
                (world_id, channel, limit))
        return [dict(r) for r in reversed(rows)]


class EventRepo:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def append(self, world_id: str, event: dict) -> None:
        await self.db.execute(
            'INSERT INTO events(world_id,id,turn_id,game_time,kind,actor_ids,changes,evidence_ids,player_visible_summary) '
            'VALUES (?,?,?,?,?,?,?,?,?)',
            (
                world_id, event['id'], event['turn_id'], event['game_time'], event['kind'],
                _json(event.get('actor_ids', [])), _json(event.get('changes', [])),
                _json(event.get('evidence_ids', [])), event.get('player_visible_summary', ''),
            ),
        )

    async def recent(self, world_id: str, limit: int = 20) -> list[dict]:
        rows = await fetchall(self.db, 
            'SELECT * FROM events WHERE world_id=? ORDER BY rowid DESC LIMIT ?',
            (world_id, limit),
        )
        return [dict(r) for r in rows]


class CheckRepo:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def get(self, world_id: str, check_id: str) -> dict | None:
        rows = await fetchall(self.db, 
            'SELECT * FROM checks WHERE world_id=? AND check_id=?', (world_id, check_id)
        )
        if not rows:
            return None
        r = rows[0]
        return {
            'check_id': r['check_id'], 'turn_id': r['turn_id'], 'input_hash': r['input_hash'],
            'ruleset_version': r['ruleset_version'], 'dice': _load(r['dice'], []),
            'payload': _load(r['payload'], {}),
        }

    async def save(self, world_id: str, check: dict) -> None:
        await self.db.execute(
            'INSERT INTO checks(world_id,check_id,turn_id,input_hash,ruleset_version,dice,payload) '
            'VALUES (?,?,?,?,?,?,?) ON CONFLICT(world_id,check_id) DO NOTHING',
            (
                world_id, check['check_id'], check['turn_id'], check['input_hash'],
                check['ruleset_version'], _json(check.get('dice', [])), _json(check.get('payload', {})),
            ),
        )


class JobRepo:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def enqueue(self, world_id: str, job_type: str, payload: dict, dedup_key: str) -> None:
        await self.db.execute(
            'INSERT OR IGNORE INTO outbox(id,world_id,dedup_key,job_type,payload,status,attempts,next_attempt_at,created_at) '
            'VALUES (?,?,?,?,?,?,?,?,?)',
            (
                str(uuid.uuid4()), world_id, dedup_key, job_type, _json(payload), 'pending', 0,
                datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(),
            ),
        )

    async def claim(self, limit: int = 5) -> list[dict]:
        now = datetime.now(timezone.utc).isoformat()
        rows = await fetchall(self.db, 
            "SELECT * FROM outbox WHERE status='pending' AND (next_attempt_at IS NULL OR next_attempt_at<=?) "
            'ORDER BY rowid LIMIT ?',
            (now, limit),
        )
        jobs = []
        for r in rows:
            await self.db.execute(
                "UPDATE outbox SET status='running', attempts=attempts+1 WHERE id=? AND status='pending'",
                (r['id'],),
            )
            jobs.append({
                'id': r['id'], 'world_id': r['world_id'], 'job_type': r['job_type'],
                'payload': _load(r['payload'], {}), 'attempts': r['attempts'] + 1,
                'dedup_key': r['dedup_key'],
            })
        return jobs

    async def complete(self, job_id: str) -> None:
        await self.db.execute("UPDATE outbox SET status='done' WHERE id=?", (job_id,))

    async def fail(self, job_id: str, next_attempt_at: str) -> None:
        await self.db.execute(
            "UPDATE outbox SET status='pending', next_attempt_at=? WHERE id=?",
            (next_attempt_at, job_id),
        )


class StreamRepo:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def next_sequence(self, world_id: str) -> int:
        rows = await fetchall(self.db, 
            'SELECT COALESCE(MAX(sequence), 0) FROM stream_events WHERE world_id=?', (world_id,)
        )
        return int(rows[0][0]) + 1

    async def append(self, world_id: str, event_type: str, payload: dict,
                     visibility: str = 'player', request_id: str | None = None) -> int:
        seq = await self.next_sequence(world_id)
        await self.db.execute(
            'INSERT INTO stream_events(world_id,sequence,event_type,request_id,payload,visibility) '
            'VALUES (?,?,?,?,?,?)',
            (world_id, seq, event_type, request_id, _json(payload), visibility),
        )
        return seq

    async def after(self, world_id: str, after_sequence: int) -> list[dict]:
        rows = await fetchall(self.db, 
            'SELECT * FROM stream_events WHERE world_id=? AND sequence>? ORDER BY sequence',
            (world_id, after_sequence),
        )
        return [
            {
                'id': r['sequence'], 'type': r['event_type'], 'world_id': r['world_id'],
                'request_id': r['request_id'], 'payload': _load(r['payload'], {}),
            }
            for r in rows
        ]


class SummaryRepo:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def insert(self, world_id: str, summary: dict) -> None:
        await self.db.execute(
            'INSERT INTO summaries(world_id,id,event_ids,entity_ids,audience_id,text,covered_until_sequence) '
            'VALUES (?,?,?,?,?,?,?)',
            (
                world_id, summary['id'], _json(summary['event_ids']), _json(summary['entity_ids']),
                summary['audience_id'], summary['text'], summary['covered_until_sequence'],
            ),
        )
        for eid in summary.get('entity_ids', []):
            await self.db.execute(
                'INSERT OR IGNORE INTO summary_entities(world_id,summary_id,entity_id) VALUES (?,?,?)',
                (world_id, summary['id'], eid),
            )


class EncounterRepo:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def get(self, world_id: str) -> dict | None:
        rows = await fetchall(self.db, 'SELECT payload FROM world_encounters WHERE world_id=?', (world_id,))
        return _load(rows[0]['payload']) if rows else None

    async def save(self, world_id: str, state: dict) -> None:
        await self.db.execute(
            'INSERT INTO world_encounters(world_id,payload) VALUES (?,?) '
            'ON CONFLICT(world_id) DO UPDATE SET payload=excluded.payload',
            (world_id, _json(state)),
        )
