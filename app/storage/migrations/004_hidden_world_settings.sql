CREATE TABLE hidden_world_settings (
  world_id TEXT PRIMARY KEY REFERENCES worlds(id) ON DELETE CASCADE
);
