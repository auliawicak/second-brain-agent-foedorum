"""Numbered SQL migrations applied in order at startup.

Every schema change is an entry in MIGRATIONS. Migrations run inside a
transaction and each successful run records its version in the
`schema_version` table. Do not hand-edit production SQLite — always add a
new numbered migration here.

Migration scripts must be idempotent (use `IF NOT EXISTS`) so that a
partially-applied migration safely retries on the next boot.
"""

from __future__ import annotations

MIGRATION_1_BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    priority TEXT NOT NULL DEFAULT 'medium',
    due_date TEXT,
    category TEXT DEFAULT 'general',
    created_at TEXT NOT NULL,
    completed_at TEXT,
    recurring_cron TEXT
);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    tags TEXT DEFAULT '[]',
    category TEXT DEFAULT 'general',
    created_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    content,
    tags,
    category,
    content='notes',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
    INSERT INTO notes_fts(rowid, content, tags, category)
    VALUES (new.id, new.content, new.tags, new.category);
END;

CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, content, tags, category)
    VALUES ('delete', old.id, old.content, old.tags, old.category);
END;

CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, content, tags, category)
    VALUES ('delete', old.id, old.content, old.tags, old.category);
    INSERT INTO notes_fts(rowid, content, tags, category)
    VALUES (new.id, new.content, new.tags, new.category);
END;

CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message TEXT NOT NULL,
    trigger_time TEXT NOT NULL,
    is_recurring INTEGER DEFAULT 0,
    cron_expression TEXT,
    is_active INTEGER DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    timestamp TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    raw_content TEXT NOT NULL,
    delivered_at TEXT
);

CREATE TABLE IF NOT EXISTS preferences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    value TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

MIGRATION_2_HEARTBEAT = """
CREATE TABLE IF NOT EXISTS heartbeat (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_seen TEXT NOT NULL
);
"""

MIGRATION_3_MODEL_HEALTH = """
CREATE TABLE IF NOT EXISTS model_health (
    model_id TEXT PRIMARY KEY,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    cooldown_until TEXT,
    last_error TEXT,
    last_success TEXT
);

CREATE TABLE IF NOT EXISTS model_usage (
    model_id TEXT NOT NULL,
    day TEXT NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    prompt_bytes INTEGER NOT NULL DEFAULT 0,
    output_tokens_est INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (model_id, day)
);
"""

MIGRATION_4_FEEDBACK = """
CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    trigger TEXT NOT NULL,            -- 'explicit' | 'edit' | 'thumbs_down'
    user_message TEXT,                -- what the user said
    agent_action TEXT,                -- what the agent did (tool + args, or reply excerpt)
    correction TEXT,                  -- what the user wanted instead
    consolidated INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    message_ref TEXT NOT NULL,
    rating INTEGER NOT NULL,          -- +1 | -1
    model_id TEXT,
    tier TEXT,
    note TEXT
);

CREATE INDEX IF NOT EXISTS idx_corrections_created ON corrections(created_at);
CREATE INDEX IF NOT EXISTS idx_feedback_created ON feedback(created_at);
"""

MIGRATIONS: list[tuple[int, str]] = [
    (1, MIGRATION_1_BASE_SCHEMA),
    (2, MIGRATION_2_HEARTBEAT),
    (3, MIGRATION_3_MODEL_HEALTH),
    (4, MIGRATION_4_FEEDBACK),
]