CREATE TABLE IF NOT EXISTS factory_slice_sessions (
    session_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS factory_slice_runs (
    run_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    revision INTEGER NOT NULL,
    document TEXT NOT NULL
);
INSERT INTO factory_slice_migrations (version) VALUES (1);
