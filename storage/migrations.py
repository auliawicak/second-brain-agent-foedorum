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

MIGRATIONS: list[tuple[int, str]] = [
    (1, MIGRATION_1_BASE_SCHEMA),
    (2, MIGRATION_2_HEARTBEAT),
]