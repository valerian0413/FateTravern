CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS worlds (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  setting TEXT NOT NULL,
  phase TEXT NOT NULL,
  version INTEGER NOT NULL,
  clock INTEGER NOT NULL,
  ruleset_id TEXT NOT NULL,
  ruleset_config TEXT NOT NULL,
  player_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entities (
  world_id TEXT NOT NULL,
  id TEXT NOT NULL,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  attributes TEXT NOT NULL,
  revision INTEGER NOT NULL,
  source_event_id TEXT,
  location_id TEXT,
  PRIMARY KEY (world_id, id),
  FOREIGN KEY (world_id) REFERENCES worlds(id)
);

CREATE TABLE IF NOT EXISTS facts (
  world_id TEXT NOT NULL,
  id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  predicate TEXT NOT NULL,
  value TEXT NOT NULL,
  status TEXT NOT NULL,
  source_event_id TEXT,
  supersedes_id TEXT,
  disclosure_rule TEXT,
  PRIMARY KEY (world_id, id),
  FOREIGN KEY (world_id) REFERENCES worlds(id)
);

CREATE TABLE IF NOT EXISTS knowledge (
  world_id TEXT NOT NULL,
  id TEXT NOT NULL,
  observer_id TEXT NOT NULL,
  fact_id TEXT,
  belief_text TEXT,
  basis TEXT NOT NULL,
  source_event_id TEXT NOT NULL,
  learned_at INTEGER NOT NULL,
  PRIMARY KEY (world_id, id),
  FOREIGN KEY (world_id) REFERENCES worlds(id)
);

CREATE TABLE IF NOT EXISTS relations (
  world_id TEXT NOT NULL,
  id TEXT NOT NULL,
  source_entity_id TEXT NOT NULL,
  target_entity_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  description TEXT NOT NULL,
  evidence_event_ids TEXT NOT NULL,
  PRIMARY KEY (world_id, id),
  FOREIGN KEY (world_id) REFERENCES worlds(id)
);

CREATE TABLE IF NOT EXISTS npc_cards (
  world_id TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  identity TEXT NOT NULL,
  traits TEXT NOT NULL,
  goals TEXT NOT NULL,
  boundaries TEXT NOT NULL,
  current_activity TEXT NOT NULL,
  location_id TEXT,
  condition TEXT NOT NULL,
  PRIMARY KEY (world_id, entity_id),
  FOREIGN KEY (world_id, entity_id) REFERENCES entities(world_id, id)
);

CREATE TABLE IF NOT EXISTS turns (
  world_id TEXT NOT NULL,
  id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  base_version INTEGER NOT NULL,
  stage TEXT NOT NULL,
  checkpoint TEXT NOT NULL,
  result TEXT,
  error_code TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (world_id, id),
  UNIQUE (world_id, request_id),
  FOREIGN KEY (world_id) REFERENCES worlds(id)
);

CREATE TABLE IF NOT EXISTS checks (
  world_id TEXT NOT NULL,
  check_id TEXT NOT NULL,
  turn_id TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  ruleset_version TEXT NOT NULL,
  dice TEXT NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY (world_id, check_id),
  UNIQUE (world_id, check_id),
  FOREIGN KEY (world_id) REFERENCES worlds(id)
);

CREATE TABLE IF NOT EXISTS proposals (
  world_id TEXT NOT NULL,
  id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  base_version INTEGER NOT NULL,
  original_text TEXT NOT NULL,
  proposed_text TEXT NOT NULL,
  fact_candidates TEXT NOT NULL,
  issues TEXT NOT NULL,
  status TEXT NOT NULL,
  PRIMARY KEY (world_id, id),
  FOREIGN KEY (world_id) REFERENCES worlds(id)
);

CREATE TABLE IF NOT EXISTS messages (
  world_id TEXT NOT NULL,
  id TEXT NOT NULL,
  channel TEXT NOT NULL,
  author TEXT NOT NULL,
  text TEXT NOT NULL,
  sequence INTEGER NOT NULL,
  request_id TEXT,
  turn_id TEXT,
  PRIMARY KEY (world_id, id),
  UNIQUE (world_id, sequence),
  FOREIGN KEY (world_id) REFERENCES worlds(id)
);

CREATE TABLE IF NOT EXISTS events (
  world_id TEXT NOT NULL,
  id TEXT NOT NULL,
  turn_id TEXT NOT NULL,
  game_time INTEGER NOT NULL,
  kind TEXT NOT NULL,
  actor_ids TEXT NOT NULL,
  changes TEXT NOT NULL,
  evidence_ids TEXT NOT NULL,
  player_visible_summary TEXT NOT NULL,
  PRIMARY KEY (world_id, id),
  FOREIGN KEY (world_id) REFERENCES worlds(id)
);

CREATE TABLE IF NOT EXISTS summaries (
  world_id TEXT NOT NULL,
  id TEXT NOT NULL,
  event_ids TEXT NOT NULL,
  entity_ids TEXT NOT NULL,
  audience_id TEXT NOT NULL,
  text TEXT NOT NULL,
  covered_until_sequence INTEGER NOT NULL,
  PRIMARY KEY (world_id, id),
  FOREIGN KEY (world_id) REFERENCES worlds(id)
);

CREATE TABLE IF NOT EXISTS summary_entities (
  world_id TEXT NOT NULL,
  summary_id TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  PRIMARY KEY (world_id, summary_id, entity_id)
);

CREATE TABLE IF NOT EXISTS scheduled_events (
  world_id TEXT NOT NULL,
  id TEXT NOT NULL,
  due_at INTEGER NOT NULL,
  prerequisites TEXT NOT NULL,
  actor_ids TEXT NOT NULL,
  intent TEXT NOT NULL,
  status TEXT NOT NULL,
  source_event_id TEXT,
  PRIMARY KEY (world_id, id),
  FOREIGN KEY (world_id) REFERENCES worlds(id)
);

CREATE TABLE IF NOT EXISTS outbox (
  id TEXT PRIMARY KEY,
  world_id TEXT NOT NULL,
  dedup_key TEXT NOT NULL UNIQUE,
  job_type TEXT NOT NULL,
  payload TEXT NOT NULL,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL,
  next_attempt_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stream_events (
  world_id TEXT NOT NULL,
  sequence INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  request_id TEXT,
  payload TEXT NOT NULL,
  visibility TEXT NOT NULL,
  PRIMARY KEY (world_id, sequence),
  FOREIGN KEY (world_id) REFERENCES worlds(id)
);
