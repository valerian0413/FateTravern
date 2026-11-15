"""Shared domain types. IDs are UUID strings; game time is non-negative minutes UTC clock."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Phase(str, Enum):
    PREPARED = 'prepared'
    PLAYING = 'playing'


class EntityKind(str, Enum):
    PLAYER = 'player'
    NPC = 'npc'
    PLACE = 'place'
    ITEM = 'item'
    FACTION = 'faction'
    CLUE = 'clue'
    QUEST = 'quest'


class FactStatus(str, Enum):
    ACTIVE = 'active'
    SUPERSEDED = 'superseded'


class KnowledgeBasis(str, Enum):
    OBSERVED = 'observed'
    REPORTED = 'reported'
    INFERRED = 'inferred'


class Channel(str, Enum):
    INTERACTION = 'interaction'
    NARRATIVE = 'narrative'


class IntentKind(str, Enum):
    DISCUSSION = 'discussion'
    EXPLORE = 'explore'
    COCREATE = 'cocreate'
    CLARIFY = 'clarify'
    ENCOUNTER = 'encounter'


class ProposalStatus(str, Enum):
    PENDING = 'pending'
    ACCEPTED = 'accepted'
    REJECTED = 'rejected'
    EXPIRED = 'expired'


class ChallengeMode(str, Enum):
    AUTOMATIC = 'automatic'
    CHECK = 'check'
    BLOCKED = 'blocked'


class IssueSeverity(str, Enum):
    BLOCKING = 'blocking'
    WARNING = 'warning'


class TurnStage(str, Enum):
    RECEIVED = 'received'
    INTERPRETING = 'interpreting'
    DISCUSSING = 'discussing'
    AWAITING_CONFIRMATION = 'awaiting_confirmation'
    PREPARING = 'preparing'
    RESOLVING = 'resolving'
    NARRATING = 'narrating'
    VALIDATING = 'validating'
    COMMITTING = 'committing'
    COMPLETED = 'completed'
    FAILED_RETRYABLE = 'failed_retryable'
    STALE = 'stale'
    CANCELLED = 'cancelled'


def to_dict(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, '__dataclass_fields__'):
        return {k: to_dict(v) for k, v in asdict(value).items()}
    if isinstance(value, list):
        return [to_dict(v) for v in value]
    if isinstance(value, dict):
        return {k: to_dict(v) for k, v in value.items()}
    return value


@dataclass
class World:
    id: str
    name: str
    setting: str
    phase: Phase
    version: int
    clock: int
    ruleset_id: str
    ruleset_config: dict
    player_id: str


@dataclass
class Entity:
    id: str
    world_id: str
    kind: EntityKind
    name: str
    attributes: dict
    revision: int
    source_event_id: str | None = None


@dataclass
class Fact:
    id: str
    world_id: str
    subject_id: str
    predicate: str
    value: Any
    status: FactStatus
    source_event_id: str | None = None
    supersedes_id: str | None = None
    disclosure_rule: dict | None = None


@dataclass
class Knowledge:
    id: str
    world_id: str
    observer_id: str
    basis: KnowledgeBasis
    source_event_id: str
    learned_at: int
    fact_id: str | None = None
    belief_text: str | None = None


@dataclass
class Relation:
    id: str
    source_entity_id: str
    target_entity_id: str
    kind: str
    description: str
    evidence_event_ids: list[str] = field(default_factory=list)


@dataclass
class NpcCard:
    entity_id: str
    identity: str
    traits: list[str]
    goals: list[str]
    boundaries: list[str]
    current_activity: str
    location_id: str | None
    condition: str


@dataclass
class Intent:
    kind: IntentKind
    target_ids: list[str]
    creation_statements: list[str]
    needs_clarification: bool
    requested_tools: list[str]
    normalized_action: str | None = None
    question: str | None = None
    unresolved: list[str] = field(default_factory=list)
    encounter_kind: str | None = None


@dataclass
class Proposal:
    id: str
    revision: int
    world_id: str
    base_version: int
    original_text: str
    proposed_text: str
    fact_candidates: list[dict]
    issues: list[dict]
    status: ProposalStatus


@dataclass
class CandidateBatch:
    temp_entities: list[dict]
    facts: list[dict]
    npc_intents: list[dict]
    challenges: list[dict]
    changes: list[dict]
    unresolved_refs: list[str]


@dataclass
class Issue:
    severity: IssueSeverity
    path: str
    reason: str
    suggested_fix: str
    reference_id: str | None = None


@dataclass
class Turn:
    id: str
    request_id: str
    payload_hash: str
    world_id: str
    base_version: int
    stage: TurnStage
    checkpoint: dict
    result: dict | None = None
    error_code: str | None = None
    created_at: str = ''


@dataclass
class ScheduledEvent:
    id: str
    world_id: str
    due_at: int
    prerequisites: list[str]
    actor_ids: list[str]
    intent: str
    status: str
    source_event_id: str | None = None


@dataclass
class Summary:
    id: str
    world_id: str
    event_ids: list[str]
    entity_ids: list[str]
    audience_id: str
    text: str
    covered_until_sequence: int


@dataclass
class DisclosureGrant:
    fact_id: str
    observer_id: str
    turn_id: str
    evidence_ids: list[str]
    check_id: str | None = None


@dataclass
class PublicOutcome:
    authorized_action: str
    observable_events: list[dict]
    visible_entities: list[dict]
    visible_facts: list[dict]
    stop_reason: str


@dataclass
class TurnDraft:
    base_version: int
    registered_candidates: dict
    resolution_ids: list[str]
    changes: list[dict]
    events: list[dict]
    public_outcome: PublicOutcome
    narration: dict
    next_jobs: list[dict]
    options: list[str] = field(default_factory=list)
    choice_prompt: str = ''
    check_result: dict | None = None
    encounter: dict | None = None
    combat_decision: dict | None = None
    encounter_checks: list[dict] = field(default_factory=list)


@dataclass
class Snapshot:
    world: World
    entities: dict[str, Entity]
    facts: dict[str, Fact]
    knowledge: dict[str, Knowledge]
    relations: dict[str, Relation]
    npc_cards: dict[str, NpcCard]
    scheduled: dict[str, ScheduledEvent]


CHANGE_OPS = frozenset({
    'mark_stress', 'clear_stress', 'take_consequence', 'taken_out', 'concede',
    'encounter_concede',
    'update_npc_card',
    'create_entity',
    'set_attribute',
    'resource_delta',
    'currency_delta',
    'transfer_item',
    'append_fact',
    'supersede_fact',
    'learn',
    'relate',
    'schedule',
    'finish_schedule',
})
