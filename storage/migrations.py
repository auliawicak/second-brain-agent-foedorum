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

MIGRATION_5_PREFERENCE_LOOP = """
-- Phase 6 §6.1 — learning loop: extend preferences with consolidation
-- semantics (fact/category/keywords/evidence/confidence/core/supersession)
-- plus a full-text index for retrieval, and a versioned persona table for
-- the monthly operating-principles proposal (§6.5).

ALTER TABLE preferences ADD COLUMN fact TEXT;
ALTER TABLE preferences ADD COLUMN category TEXT NOT NULL DEFAULT 'personal';
ALTER TABLE preferences ADD COLUMN keywords TEXT NOT NULL DEFAULT '';
ALTER TABLE preferences ADD COLUMN confidence REAL NOT NULL DEFAULT 0.5;
ALTER TABLE preferences ADD COLUMN evidence_count INTEGER NOT NULL DEFAULT 1;
ALTER TABLE preferences ADD COLUMN first_seen TEXT;
ALTER TABLE preferences ADD COLUMN last_seen TEXT;
ALTER TABLE preferences ADD COLUMN is_core INTEGER NOT NULL DEFAULT 0;
ALTER TABLE preferences ADD COLUMN superseded_by INTEGER REFERENCES preferences(id);
ALTER TABLE preferences ADD COLUMN source_refs TEXT;

UPDATE preferences SET fact = value,
                       confidence = 0.7,
                       evidence_count = 1,
                       is_core = 0,
                       first_seen = created_at,
                       last_seen = created_at
 WHERE fact IS NULL OR fact = '';

CREATE VIRTUAL TABLE IF NOT EXISTS preferences_fts USING fts5(
    fact, keywords, content='preferences', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS preferences_ai AFTER INSERT ON preferences BEGIN
    INSERT INTO preferences_fts(rowid, fact, keywords)
    VALUES (new.id, new.fact, new.keywords);
END;

CREATE TRIGGER IF NOT EXISTS preferences_ad AFTER DELETE ON preferences BEGIN
    INSERT INTO preferences_fts(preferences_fts, rowid, fact, keywords)
    VALUES ('delete', old.id, old.fact, old.keywords);
END;

CREATE TRIGGER IF NOT EXISTS preferences_au AFTER UPDATE ON preferences BEGIN
    INSERT INTO preferences_fts(preferences_fts, rowid, fact, keywords)
    VALUES ('delete', old.id, old.fact, old.keywords);
    INSERT INTO preferences_fts(rowid, fact, keywords)
    VALUES (new.id, new.fact, new.keywords);
END;

INSERT INTO preferences_fts(rowid, fact, keywords)
SELECT id, fact, keywords FROM preferences;

CREATE TABLE IF NOT EXISTS persona_versions (
    id INTEGER PRIMARY KEY,
    version INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'operating_principles',
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    applied INTEGER NOT NULL DEFAULT 1
);
"""

MIGRATION_6_NUDGES = """
-- Phase 7 §7.4 — condition-check nudges, deduplicated once per entity per
-- day (day + entity_type + entity_id + condition is unique). Plus indexes
-- that keep the 15-minute condition pass fast at 1,000+ tasks.

CREATE TABLE IF NOT EXISTS nudge_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,                -- YYYY-MM-DD (local time)
    entity_type TEXT NOT NULL,        -- 'task' | 'note' | 'day'
    entity_id INTEGER NOT NULL,       -- entity row id (or day ordinal)
    condition TEXT NOT NULL,          -- 'overdue' | 'imbalance' | 'stale'
    created_at TEXT NOT NULL,
    UNIQUE(day, entity_type, entity_id, condition)
);

CREATE INDEX IF NOT EXISTS idx_tasks_status_due ON tasks(status, due_date);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);
CREATE INDEX IF NOT EXISTS idx_notes_created ON notes(created_at);
CREATE INDEX IF NOT EXISTS idx_reminders_active ON reminders(is_active);
"""

MIGRATION_7_PERSONA = """
-- Phase 8 §8.1 — persona as DATA. The three independent layers that shape
-- the system prompt (Voice, Principles, Mode rules) live here as versioned
-- snapshots. Every edit inserts a new row with an incremented version and
-- the untouched layers carried forward; exactly one row is `active`.
-- Rollback is a pure flag flip, so tone experiments are trivially
-- reversible. A partial unique index on active=1 enforces the single
-- active invariant at the schema level.

CREATE TABLE IF NOT EXISTS persona (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL,
    voice TEXT NOT NULL,
    principles TEXT NOT NULL,
    mode_rules TEXT,
    active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_persona_single_active
    ON persona(active) WHERE active = 1;

-- Seed: persona v1 (active) — §8.1 seed voice, the six core operating
-- principles, and the work/evening mode split. Idempotent: only inserted
-- if the table is still empty (safe to re-run after a partial apply).
INSERT INTO persona (version, voice, principles, mode_rules, active, created_at)
SELECT 1,
    'Mix Bahasa Indonesia and English naturally; use English for technical '
    || 'and business terms. Professional but not stiff. Minimal emoji.',
    '- Default to doing, not offering. Never end with "would you like me to…" '
    || '- do it and report what you did.' || char(10)
    || '- Never ask for something you can look up in the database.' || char(10)
    || '- If the user contradicts something previously stored, say so out '
    || 'loud rather than silently overwriting.' || char(10)
    || '- Push back when a plan has a hole. Do not agree by default.' || char(10)
    || '- Match the user''s language.' || char(10)
    || '- If a request is vague, make a reasonable guess and label it as a guess.',
    'Work hours (06:00–18:00): terse, action-first. Evening: reflective.',
    1,
    datetime('now')
WHERE NOT EXISTS (SELECT 1 FROM persona);
"""

MIGRATIONS: list[tuple[int, str]] = [
    (1, MIGRATION_1_BASE_SCHEMA),
    (2, MIGRATION_2_HEARTBEAT),
    (3, MIGRATION_3_MODEL_HEALTH),
    (4, MIGRATION_4_FEEDBACK),
    (5, MIGRATION_5_PREFERENCE_LOOP),
    (6, MIGRATION_6_NUDGES),
    (7, MIGRATION_7_PERSONA),
]