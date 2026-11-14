CREATE TABLE IF NOT EXISTS world_encounters (
    world_id TEXT PRIMARY KEY REFERENCES worlds(id) ON DELETE CASCADE,
    payload TEXT NOT NULL
);
