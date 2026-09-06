"""SQLite database layer with async access and FTS5 full-text search."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite

from config import Config
from storage.migrations import MIGRATIONS
from storage.models import (
    ConversationEntry,
    Note,
    NoteCreate,
    Preference,
    PreferenceCreate,
    Reminder,
    ReminderCreate,
    Task,
    TaskCreate,
    TaskStatus,
)

logger = logging.getLogger(__name__)


# ─── Phase 6 text helpers ──────────────────────────────────────────────
# Used by the learning-loop retrieval & merging in Database.merge_fact.

# Tokens that only add noise during rephrasing, dropped when comparing
# candidate fact token-sets for a merge (kept separate from _NORMALIZE_DROP
# so they don't influence the stored/displayed fact).
_MATCH_CRUFT = frozenset(
    {
        "to", "in", "at", "on", "with", "for", "and", "or", "the", "a", "an",
        "each", "every", "usually", "often", "sometimes", "about", "from",
        "into", "by", "of",
    }
)


# Tokens dropped during normalization: negation markers, softening pronouns
# and the helper verbs that only copycat a negation. Word-boundary only —
# "no"/"never" never touch "now"/"nevermind".
_NORMALIZE_DROP = frozenset(
    {
        "i", "you", "we", "they", "he", "she", "the", "user", "my", "does",
        "do", "did", "please", "actually", "honestly", "no", "not", "none",
        "never", "don't", "dont", "doesn't", "doesnt", "didn't", "didnt",
        "won't", "wont", "can't", "cant", "cannot", "isn't", "isnt",
        "aren't", "arent", "ain't", "while", "without", "longer", "noone",
        "dislike", "hate", "avoid", "stop", "skip",
    }
)


def normalize_fact(text: str) -> str:
    """Lowercase, drop negation/softener/helper tokens (word-boundary) and
    collapse whitespace, so 'I like to run every morning' ≈ 'you prefer to run
    each morning', and 'do not like X' normalizes to the same base as 'like X'
    — letting a negated form supersede its positive counterpart (§6.2)."""
    tokens = re.findall(r"[a-z0-9']+", (text or "").lower())
    kept = [t for t in tokens if t not in _NORMALIZE_DROP]
    return " ".join(kept)


def contains_negation(text: str) -> bool:
    tokens = set(re.findall(r"[a-z0-9']+", (text or "").lower()))
    return bool(tokens & {
        "no", "not", "none", "never", "don't", "dont", "doesn't", "doesnt",
        "didn't", "didnt", "won't", "wont", "can't", "cant", "cannot",
        "isn't", "isnt", "aren't", "arent", "ain't", "without", "noone",
        "dislike", "hate", "avoid", "stop", "skip",
    })


# Phrases that give key/value style guidance for a preference fact.
KEYWORD_HINTS = (
    "he prefers", "you prefer", "the user prefers", "prefers",
    "has a habit", "is a habit", "loves", "loves to", "likes to",
    "hates", "dislikes", "avoids", "wants to", "wants",
)


def extract_keywords(fact: str) -> str:
    """Derive a lightweight keyword string for FTS from the fact itself —
    used when the consolidator supplies none."""
    t = re.sub(r"\b(i|you|we|they|he|she|the user|my user|really|always|never)\b", " ",
               (fact or "").lower())
    return " ".join(re.findall(r"[a-z0-9']{3,}", t)[:8])


def keyword_query(text: str) -> str:
    """Build an FTS5-safe MATCH query.

    Tokens are quoted (so apostrophes and stopwords can't corrupt the
    expression) and joined with OR for recall-first matching; bm25 ranking
    keeps precision. Negation and stopword tokens are dropped so a query like
    "what coffee should I buy?" contributes only `"coffee" OR "buy"`.
    """
    tokens = []
    for tok in re.findall(r"[a-z0-9']+", (text or "").lower()):
        tok = tok.rstrip("'s")
        if tok in _FTS_STOPWORDS or contains_negation(tok):
            continue
        if len(tok) < 3:
            continue
        tokens.append(f'"{tok}"')
    return " OR ".join(tokens)


_FTS_STOPWORDS = {
    "about", "after", "again", "all", "also", "and", "any", "are", "because",
    "been", "before", "being", "both", "but", "can", "could", "did", "does",
    "doing", "down", "during", "each", "few", "for", "from", "further",
    "had", "has", "have", "having", "here", "how", "into", "its", "just",
    "more", "most", "much", "must", "now", "once", "only", "other", "our",
    "ours", "out", "over", "same", "some", "such", "than", "that", "the",
    "their", "them", "then", "there", "these", "they", "this", "those",
    "through", "to", "too", "under", "until", "up", "very", "was", "were",
    "what", "when", "where", "which", "while", "why", "with", "would",
    "you", "your", "yours",
}


class Database:
    """Async SQLite database for the Second Brain.

    Provides CRUD operations for tasks, notes, reminders, conversations,
    and daily digests, with FTS5 full-text search on notes.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Open the database connection and apply pending schema migrations."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._apply_migrations()
        logger.info("Database connected: %s", self.db_path)

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None
            logger.info("Database closed.")

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._db

    # ─── Migrations ──────────────────────────────────────────────────────

    async def _apply_migrations(self) -> None:
        """Apply pending schema migrations in order.

        Tracks applied migrations in the `schema_version` table. Each
        migration runs in a transaction; a failure rolls back and is
        retried (idempotent DDL) on the next startup.
        """
        await self.db.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
        )
        cursor = await self.db.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_version"
        )
        row = await cursor.fetchone()
        current = int(row[0])

        for version, sql in MIGRATIONS:
            if version <= current:
                continue
            logger.info("Applying schema migration %d...", version)
            await self.db.execute("BEGIN")
            try:
                await self.db.executescript(sql)
                await self.db.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (version,)
                )
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                logger.exception("Schema migration %d failed", version)
                raise
            logger.info("Schema migration %d applied.", version)

    # ─── Tasks ────────────────────────────────────────────────────────────

    async def add_task(self, task: TaskCreate) -> Task:
        """Create a new task and return it with its assigned ID."""
        now = datetime.now().isoformat()
        cursor = await self.db.execute(
            """INSERT INTO tasks (description, status, priority, due_date, category, created_at, recurring_cron)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                task.description,
                TaskStatus.PENDING.value,
                task.priority.value,
                task.due_date,
                task.category,
                now,
                task.recurring_cron,
            ),
        )
        await self.db.commit()
        return Task(
            id=cursor.lastrowid,
            description=task.description,
            priority=task.priority,
            due_date=task.due_date,
            category=task.category,
            created_at=now,
            recurring_cron=task.recurring_cron,
        )

    async def get_tasks(
        self,
        status: TaskStatus | None = None,
        date: str | None = None,
        category: str | None = None,
    ) -> list[Task]:
        """Retrieve tasks with optional filters."""
        query = "SELECT * FROM tasks WHERE 1=1"
        params: list = []

        if status:
            query += " AND status = ?"
            params.append(status.value)
        if date:
            query += " AND due_date = ?"
            params.append(date)
        if category:
            query += " AND category = ?"
            params.append(category)

        query += " ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, created_at DESC"

        cursor = await self.db.execute(query, params)
        rows = await cursor.fetchall()
        return [Task(**dict(row)) for row in rows]

    async def complete_task(self, task_id: int) -> Task | None:
        """Mark a task as done."""
        now = datetime.now().isoformat()
        await self.db.execute(
            "UPDATE tasks SET status = ?, completed_at = ? WHERE id = ?",
            (TaskStatus.DONE.value, now, task_id),
        )
        await self.db.commit()
        cursor = await self.db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = await cursor.fetchone()
        return Task(**dict(row)) if row else None

    async def delete_task(self, task_id: int) -> bool:
        """Delete a task by ID."""
        cursor = await self.db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        await self.db.commit()
        return cursor.rowcount > 0

    async def get_today_tasks(self, today: str) -> list[Task]:
        """Get all pending tasks due today or overdue."""
        cursor = await self.db.execute(
            """SELECT * FROM tasks
               WHERE status IN ('pending', 'in_progress')
                 AND (due_date IS NULL OR due_date <= ?)
               ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END""",
            (today,),
        )
        rows = await cursor.fetchall()
        return [Task(**dict(row)) for row in rows]

    # ─── Notes ────────────────────────────────────────────────────────────

    async def add_note(self, note: NoteCreate) -> Note:
        """Save a new note."""
        now = datetime.now().isoformat()
        tags_json = json.dumps(note.tags)
        cursor = await self.db.execute(
            "INSERT INTO notes (content, tags, category, created_at) VALUES (?, ?, ?, ?)",
            (note.content, tags_json, note.category, now),
        )
        await self.db.commit()
        return Note(
            id=cursor.lastrowid,
            content=note.content,
            tags=note.tags,
            category=note.category,
            created_at=now,
        )

    async def search_notes(self, query: str, limit: int = 20) -> list[Note]:
        """Full-text search across notes using FTS5."""
        cursor = await self.db.execute(
            """SELECT n.* FROM notes n
               JOIN notes_fts fts ON n.id = fts.rowid
               WHERE notes_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (query, limit),
        )
        rows = await cursor.fetchall()
        results = []
        for row in rows:
            data = dict(row)
            data["tags"] = json.loads(data["tags"]) if isinstance(data["tags"], str) else data["tags"]
            results.append(Note(**data))
        return results

    async def get_recent_notes(self, limit: int = 10) -> list[Note]:
        """Get the most recent notes."""
        cursor = await self.db.execute(
            "SELECT * FROM notes ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        results = []
        for row in rows:
            data = dict(row)
            data["tags"] = json.loads(data["tags"]) if isinstance(data["tags"], str) else data["tags"]
            results.append(Note(**data))
        return results

    # ─── Reminders ────────────────────────────────────────────────────────

    async def add_reminder(self, reminder: ReminderCreate) -> Reminder:
        """Create a new reminder."""
        now = datetime.now().isoformat()
        cursor = await self.db.execute(
            """INSERT INTO reminders (message, trigger_time, is_recurring, cron_expression, is_active, created_at)
               VALUES (?, ?, ?, ?, 1, ?)""",
            (
                reminder.message,
                reminder.trigger_time,
                int(reminder.is_recurring),
                reminder.cron_expression,
                now,
            ),
        )
        await self.db.commit()
        return Reminder(
            id=cursor.lastrowid,
            message=reminder.message,
            trigger_time=reminder.trigger_time,
            is_recurring=reminder.is_recurring,
            cron_expression=reminder.cron_expression,
            created_at=now,
        )

    async def get_due_reminders(self, now_iso: str) -> list[Reminder]:
        """Get all active reminders that are due (trigger_time <= now)."""
        cursor = await self.db.execute(
            """SELECT * FROM reminders
               WHERE is_active = 1 AND trigger_time <= ?
               ORDER BY trigger_time""",
            (now_iso,),
        )
        rows = await cursor.fetchall()
        return [Reminder(**dict(row)) for row in rows]

    async def deactivate_reminder(self, reminder_id: int) -> None:
        """Mark a reminder as inactive after it fires."""
        await self.db.execute(
            "UPDATE reminders SET is_active = 0 WHERE id = ?", (reminder_id,)
        )
        await self.db.commit()

    async def update_reminder_time(self, reminder_id: int, trigger_time: str) -> None:
        """Advance a reminder's trigger time (used for recurring reminders)."""
        await self.db.execute(
            "UPDATE reminders SET trigger_time = ? WHERE id = ?",
            (trigger_time, reminder_id),
        )
        await self.db.commit()

    async def get_active_reminders(self) -> list[Reminder]:
        """Get all active reminders."""
        cursor = await self.db.execute(
            "SELECT * FROM reminders WHERE is_active = 1 ORDER BY trigger_time"
        )
        rows = await cursor.fetchall()
        return [Reminder(**dict(row)) for row in rows]

    # ─── Conversations ────────────────────────────────────────────────────

    async def log_conversation(self, role: str, content: str) -> None:
        """Log a conversation message for memory search."""
        now = datetime.now().isoformat()
        await self.db.execute(
            "INSERT INTO conversations (role, content, timestamp) VALUES (?, ?, ?)",
            (role, content, now),
        )
        await self.db.commit()

    async def get_recent_conversations(self, limit: int = 20) -> list[ConversationEntry]:
        """Get recent conversation entries."""
        cursor = await self.db.execute(
            "SELECT * FROM conversations ORDER BY timestamp DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [ConversationEntry(**dict(row)) for row in reversed(list(rows))]

    # ─── Data hygiene (Phase 3) ──────────────────────────────────────────

    async def get_conversations_before(self, before_iso: str) -> list[ConversationEntry]:
        """Get conversation rows older than `before_iso` (newest first)."""
        cursor = await self.db.execute(
            """SELECT * FROM conversations
               WHERE timestamp < ? ORDER BY timestamp ASC""",
            (before_iso,),
        )
        rows = await cursor.fetchall()
        return [ConversationEntry(**dict(row)) for row in rows]

    async def delete_conversations_before(self, before_iso: str) -> int:
        """Delete conversation rows older than `before_iso`. Returns rowcount."""
        cursor = await self.db.execute(
            "DELETE FROM conversations WHERE timestamp < ?", (before_iso,)
        )
        await self.db.commit()
        return cursor.rowcount

    async def get_all_notes(self) -> list[Note]:
        """Get every note (for the markdown vault export)."""
        cursor = await self.db.execute("SELECT * FROM notes ORDER BY created_at ASC")
        rows = await cursor.fetchall()
        results = []
        for row in rows:
            data = dict(row)
            data["tags"] = json.loads(data["tags"]) if isinstance(data["tags"], str) else data["tags"]
            results.append(Note(**data))
        return results

    async def get_all_tasks(self) -> list[Task]:
        """Get every task (for the markdown vault export)."""
        cursor = await self.db.execute("SELECT * FROM tasks ORDER BY created_at ASC")
        rows = await cursor.fetchall()
        return [Task(**dict(row)) for row in rows]

    async def get_all_conversations(self) -> list[ConversationEntry]:
        """Get every conversation row (for the markdown vault export)."""
        cursor = await self.db.execute("SELECT * FROM conversations ORDER BY timestamp ASC")
        rows = await cursor.fetchall()
        return [ConversationEntry(**dict(row)) for row in rows]

    async def checkpoint_truncate(self) -> None:
        """Run PRAGMA wal_checkpoint(TRUNCATE) to compact the WAL."""
        try:
            cur = await self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            await cur.fetchall()  # consume so no statement stays open (VACUUM-safe)
            logger.debug("WAL checkpoint (TRUNCATE) ran.")
        except Exception:
            logger.exception("WAL checkpoint (TRUNCATE) failed.")

    async def vacuum(self) -> None:
        """Reclaim freed pages (run on the first Sunday of each month)."""
        await self.db.execute("VACUUM")
        logger.info("VACUUM completed.")

    # ─── Daily Digests ────────────────────────────────────────────────────

    async def save_digest(self, date: str, content: str) -> None:
        """Save a daily digest (upsert)."""
        now = datetime.now().isoformat()
        await self.db.execute(
            """INSERT INTO daily_digests (date, raw_content, delivered_at)
               VALUES (?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET raw_content = ?, delivered_at = ?""",
            (date, content, now, content, now),
        )
        await self.db.commit()

    async def get_digest(self, date: str) -> str | None:
        """Retrieve a saved digest by date."""
        cursor = await self.db.execute(
            "SELECT raw_content FROM daily_digests WHERE date = ?", (date,)
        )
        row = await cursor.fetchone()
        return row["raw_content"] if row else None

    # ─── User Preferences / Habits ────────────────────────────────────────

    async def save_preference(self, pref: PreferenceCreate) -> None:
        """Save a user preference or habit.

        Phase 6 §6.2: stores the fact with learning-loop columns. An exact
        re-statement of a live fact bumps evidence/confidence instead of
        duplicating; a contradicting statement supersedes rather than
        overwrites (matching `merge_fact` semantics). `key` is preserved for
        backward compatibility.
        """
        now = datetime.now().isoformat()
        await self.merge_fact(
            fact=pref.value,
            category="personal",
            keywords="",
            confidence=0.7,
            key=pref.key,
            evidence_ref=f"explicit:{pref.key}",
            now=now,
        )

    async def get_all_preferences(self) -> list[Preference]:
        """Get all saved user preferences (superseded rows included)."""
        cursor = await self.db.execute("SELECT * FROM preferences ORDER BY created_at")
        rows = await cursor.fetchall()
        return [Preference(**dict(row)) for row in rows]

    # ─── Phase 6 — learning loop ─────────────────────────────────────────

    @staticmethod
    async def _preference_row(raw) -> Preference:
        return Preference(**dict(raw))

    async def _insert_fact(
        self,
        *,
        fact: str,
        category: str,
        keywords: str,
        confidence: float,
        evidence_ref: str | None,
        key: str | None,
        now: str,
    ) -> int:
        cursor = await self.db.execute(
            """INSERT INTO preferences
               (key, value, created_at, fact, category, keywords, confidence,
                evidence_count, first_seen, last_seen, is_core, source_refs)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 0, ?)""",
            (
                key or fact[:40],
                fact,
                now,
                fact,
                category,
                keywords,
                confidence,
                now,
                now,
                json.dumps([evidence_ref]) if evidence_ref else None,
            ),
        )
        await self.db.commit()
        return cursor.lastrowid

    async def merge_fact(
        self,
        fact: str,
        category: str = "personal",
        keywords: str = "",
        confidence: float = 0.5,
        *,
        key: str | None = None,
        evidence_ref: str | None = None,
        now: str | None = None,
    ) -> tuple[str, int]:
        """Merge a consolidated fact into the preference store.

        Returns (outcome, preference_id) where outcome is one of:
          "new"        — inserted as a fresh preference
          "matched"    — a live preference already matches → evidence bumps
          "superseded" — contradicts a live preference → old row's
                         `superseded_by` points at the new row (history kept,
                         never overwritten, §6.2)

        Matching = normalised string equality, then FTS5 top-hit similarity.
        Contradiction = same underlying fact with a sign flip (a negation
        marker in exactly one of the two).
        """
        now = now or datetime.now().isoformat()
        fact = (fact or "").strip()
        if not fact:
            return "new", -1

        match = await self._match_active_preference(fact, category, keywords)

        if match is not None:
            row, contradiction = match
            if contradiction:
                new_id = await self._insert_fact(
                    fact=fact, category=category, keywords=keywords,
                    confidence=confidence, evidence_ref=evidence_ref,
                    key=key, now=now,
                )
                await self.db.execute(
                    "UPDATE preferences SET superseded_by = ? WHERE id = ?",
                    (new_id, row["id"]),
                )
                await self.db.commit()
                return "superseded", new_id

            await self.db.execute(
                """UPDATE preferences
                   SET evidence_count = evidence_count + 1,
                       confidence = MIN(0.95, confidence + 0.1),
                       last_seen = ?,
                       source_refs = CASE
                           WHEN ? IS NULL THEN source_refs
                           ELSE COALESCE(source_refs, '[]') END
                   WHERE id = ?""",
                (now, evidence_ref, row["id"]),
            )
            await self.db.commit()
            return "matched", row["id"]

        new_id = await self._insert_fact(
            fact=fact, category=category, keywords=keywords,
            confidence=confidence, evidence_ref=evidence_ref,
            key=key, now=now,
        )
        return "new", new_id

    async def _match_active_preference(
        self, fact: str, category: str, keywords: str
    ) -> tuple[dict, bool] | None:
        """Return (active_preference_row, is_contradiction) or None."""
        cursor = await self.db.execute(
            "SELECT * FROM preferences WHERE superseded_by IS NULL"
        )
        rows = await cursor.fetchall()

        normalized = normalize_fact(fact)
        new_negation = contains_negation(fact)
        for row in rows:
            existing = row["fact"] or row["value"] or ""
            if not existing:
                continue
            if normalize_fact(existing) != normalized:
                continue
            existing_neg = contains_negation(existing)
            if existing_neg != new_negation:
                return dict(row), True
            return dict(row), False

        # FTS5 fallback: top candidates ranked by bm25. A candidate matches when
# the cleaned token overlap is substantial — captures rephrasings
# ("prefers to run at night" ≡ "run at night", and "likes X" ≡ "prefers X")
# while keeping genuinely different facts (tea vs coffee) apart. Reports a
# contradiction when exactly one side carries a negation.
        query = keyword_query(fact)
        if not query:
            return None
        cursor = await self.db.execute(
            """SELECT p.* FROM preferences p
               JOIN preferences_fts fts ON p.id = fts.rowid
               WHERE preferences_fts MATCH ? AND p.superseded_by IS NULL
               ORDER BY bm25(preferences_fts) LIMIT 4""",
            (query,),
        )
        incoming = set(normalize_fact(fact).split())
        new_negation = contains_negation(fact)
        for row in await cursor.fetchall():
            cand = normalize_fact(row["fact"] or row["value"] or "")
            if cand == normalized:
                continue  # the very same fact — handled by the exact pass
            cand_tokens = set(cand.split())
            if not cand_tokens:
                continue
            in_clean = incoming - _MATCH_CRUFT
            cand_clean = cand_tokens - _MATCH_CRUFT
            common = in_clean & cand_clean
            if len(common) < 2:
                continue
            shorter = min(len(in_clean), len(cand_clean))
            if len(common) < shorter - 1:
                continue
            if len(common) / max(len(in_clean), len(cand_clean), 1) < 0.5:
                continue
            contradiction = contains_negation(
                row["fact"] or row["value"] or ""
            ) != new_negation
            return dict(row), contradiction
        return None

    async def get_context_preferences(self, user_message: str | None) -> list[Preference]:
        """§6.4 retrieval-over-injection.

        Returns core preferences (is_core=1, capped at
        `MAX_CORE_PREFS_INJECTED`) always present, plus the top FTS5 matches
        against `user_message` (capped at `MAX_FTS_PREFS_INJECTED`), excluding
        cores, superseded rows and rows below `MIN_PREF_CONFIDENCE`. Total is
        therefore never more than 23.
        """
        floor = Config.MIN_PREF_CONFIDENCE
        core: list[Preference] = []
        seen: set[int] = set()

        cursor = await self.db.execute(
            """SELECT * FROM preferences
               WHERE is_core = 1 AND superseded_by IS NULL AND confidence >= ?
               ORDER BY confidence DESC, evidence_count DESC LIMIT ?""",
            (floor, Config.MAX_CORE_PREFS_INJECTED),
        )
        for row in await cursor.fetchall():
            core.append(await self._preference_row(row))
            seen.add(row["id"])

        query = keyword_query(user_message or "")
        fts_rows: list[Preference] = []
        if query and seen:
            params: list[object] = [query, floor, *seen]
            placeholders = ",".join("?" for _ in seen)
            cursor = await self.db.execute(
                f"""SELECT p.* FROM preferences p
                    JOIN preferences_fts fts ON p.id = fts.rowid
                    WHERE preferences_fts MATCH ?
                      AND p.confidence >= ?
                      AND p.is_core = 0
                      AND p.superseded_by IS NULL
                      AND p.id NOT IN ({placeholders})
                    ORDER BY bm25(preferences_fts)
                    LIMIT ?""",
                [*params, Config.MAX_FTS_PREFS_INJECTED],
            )
            fts_rows = [await self._preference_row(r) for r in await cursor.fetchall()]
        elif query and not seen:
            cursor = await self.db.execute(
                """SELECT p.* FROM preferences p
                   JOIN preferences_fts fts ON p.id = fts.rowid
                   WHERE preferences_fts MATCH ?
                     AND p.confidence >= ?
                     AND p.is_core = 0
                     AND p.superseded_by IS NULL
                   ORDER BY bm25(preferences_fts)
                   LIMIT ?""",
                (query, floor, Config.MAX_FTS_PREFS_INJECTED),
            )
            fts_rows = [await self._preference_row(r) for r in await cursor.fetchall()]

        return core + fts_rows

    async def apply_decay(self, now: datetime | None = None) -> dict:
        """§6.3 weekly decay + promotion. Returns summary of affected rows."""
        now = now or datetime.now()
        cutoff = (now - timedelta(days=90)).isoformat()

        cursor = await self.db.execute(
            """UPDATE preferences SET confidence = confidence * 0.8
               WHERE last_seen < ? AND is_core = 0 AND superseded_by IS NULL""",
            (cutoff,),
        )
        decayed = cursor.rowcount

        cursor = await self.db.execute(
            """UPDATE preferences SET is_core = 1
               WHERE confidence >= 0.8 AND evidence_count >= 3
                 AND superseded_by IS NULL"""
        )
        promoted = cursor.rowcount

        cursor = await self.db.execute(
            """UPDATE preferences SET is_core = 0
               WHERE confidence < 0.6 AND is_core = 1"""
        )
        demoted = cursor.rowcount
        await self.db.commit()
        return {"decayed": decayed, "promoted": promoted, "demoted": demoted}

    async def enforce_pref_cap(self, max_preferences: int | None = None) -> int:
        """§6.2 cap: keep at most `max_preferences` live rows. Drops the
        lowest-confidence non-core rows beyond the cap; cores are never dropped.
        Returns the number of rows dropped."""
        cap = max_preferences or Config.MAX_PREFERENCES
        cursor = await self.db.execute(
            "SELECT COUNT(*) AS c FROM preferences WHERE superseded_by IS NULL"
        )
        total = (await cursor.fetchone())["c"]
        if total <= cap:
            return 0

        cursor = await self.db.execute(
            """SELECT id FROM preferences
               WHERE superseded_by IS NULL AND is_core = 0
               ORDER BY confidence ASC, evidence_count ASC, id DESC"""
        )
        ids = [r["id"] for r in await cursor.fetchall()]
        excess = total - cap
        if excess <= 0:
            return 0
        doomed = ids[:excess]
        for pid in doomed:
            await self.db.execute(
                "DELETE FROM preferences WHERE id = ?", (pid,)
            )
        await self.db.commit()
        return len(doomed)

    # ─── Corrections / evidence intake (Phase 6 §6.2) ────────────────────

    async def get_unconsolidated_corrections(self) -> list[dict]:
        cursor = await self.db.execute(
            """SELECT * FROM corrections WHERE consolidated = 0 ORDER BY created_at"""
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def mark_corrections_consolidated(self, ids: list[int]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        await self.db.execute(
            f"UPDATE corrections SET consolidated = 1 WHERE id IN ({placeholders})",
            ids,
        )
        await self.db.commit()

    async def get_corrections_since(self, since_iso: str) -> list[dict]:
        cursor = await self.db.execute(
            """SELECT * FROM corrections WHERE created_at >= ?
               ORDER BY created_at""",
            (since_iso,),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def get_conversations_since(self, since_iso: str) -> list[ConversationEntry]:
        cursor = await self.db.execute(
            """SELECT * FROM conversations WHERE timestamp >= ?
               ORDER BY timestamp""",
            (since_iso,),
        )
        rows = await cursor.fetchall()
        return [
            ConversationEntry(
                id=r["id"], role=r["role"], content=r["content"], timestamp=r["timestamp"]
            )
            for r in rows
        ]

    async def get_tasks_completed_since(self, since_iso: str) -> list[dict]:
        cursor = await self.db.execute(
            """SELECT description, completed_at FROM tasks
               WHERE status = 'done' AND completed_at >= ?
               ORDER BY completed_at""",
            (since_iso,),
        )
        return [dict(r) for r in await cursor.fetchall()]

    # ─── Versioned persona (Phase 6 §6.5) ─────────────────────────────────

    async def get_active_persona(self, kind: str = "operating_principles") -> str | None:
        """Return the currently applied persona block for `kind`, or None."""
        cursor = await self.db.execute(
            """SELECT content FROM persona_versions
               WHERE kind = ? AND applied = 1 ORDER BY version DESC LIMIT 1""",
            (kind,),
        )
        row = await cursor.fetchone()
        return row["content"] if row else None

    async def save_persona_version(
        self, content: str, kind: str = "operating_principles"
    ) -> int:
        """Append a persona version as *inactive* (applied=0). Companions to
        §6.5: a draft only becomes active when the owner approves it via
        `set_persona_applied`. Returns the new version number."""
        now = datetime.now().isoformat()
        cur = await self.db.execute(
            "SELECT COALESCE(MAX(version), 0) FROM persona_versions WHERE kind = ?",
            (kind,),
        )
        prev = (await cur.fetchone())[0]
        await self.db.execute(
            """INSERT INTO persona_versions (version, kind, content, created_at, applied)
               VALUES (?, ?, ?, ?, 0)""",
            (prev + 1, kind, content, now),
        )
        await self.db.commit()
        return prev + 1

    async def get_persona_version(self, version_id: int) -> dict | None:
        cursor = await self.db.execute(
            """SELECT id, version, kind, content, created_at, applied
               FROM persona_versions WHERE id = ?""",
            (version_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def set_persona_applied(self, version_id: int, applied: int) -> None:
        """Flip a persona version's applied flag. Deactivating only happens
        on an explicit approve (so a mere reject never evicts the current
        active block); `applied=1` versions are mutually exclusive per kind."""
        row = await self.get_persona_version(version_id)
        if not row:
            return
        if applied:
            await self.db.execute(
                """UPDATE persona_versions SET applied = 0
                   WHERE kind = ? AND id != ?""",
                (row["kind"], version_id),
            )
            await self.db.execute(
                "UPDATE persona_versions SET applied = 1 WHERE id = ?",
                (version_id,),
            )
        else:
            await self.db.execute(
                "UPDATE persona_versions SET applied = 0 WHERE id = ?",
                (version_id,),
            )
        await self.db.commit()

    async def get_all_persona_versions(self, kind: str = "operating_principles") -> list[dict]:
        cursor = await self.db.execute(
            """SELECT id, version, kind, content, created_at, applied
               FROM persona_versions WHERE kind = ? ORDER BY version""",
            (kind,),
        )
        return [dict(r) for r in await cursor.fetchall()]

    # ─── Persona-as-data (Phase 8 §8.1) ───────────────────────────────────

    async def get_active_persona_config(self) -> dict | None:
        """Return the active persona snapshot (the single `active=1` row),
        or None if the table is empty (never on a migrated DB)."""
        cursor = await self.db.execute(
            """SELECT id, version, voice, principles, mode_rules, created_at
               FROM persona WHERE active = 1 LIMIT 1"""
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_persona_snapshot(self, version: int) -> dict | None:
        """Return a specific persona snapshot by version (for rollback)."""
        cursor = await self.db.execute(
            """SELECT id, version, voice, principles, mode_rules, created_at
               FROM persona WHERE version = ? LIMIT 1""",
            (version,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_persona_versions(self, limit: int = 20) -> list[dict]:
        """List persona snapshot history, newest first, for /persona history."""
        cursor = await self.db.execute(
            """SELECT id, version, voice, principles, mode_rules, active, created_at
               FROM persona ORDER BY version DESC LIMIT ?""",
            (limit,),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def save_persona_snapshot(
        self,
        voice: str | None = None,
        principles: str | None = None,
        mode_rules: str | None = None,
    ) -> dict:
        """Insert a new inactive persona snapshot. Fields not provided are
        carried forward verbatim from the current active row. Returns the
        new snapshot dict."""
        current = await self.get_active_persona_config()
        if current is None:
            raise RuntimeError("No active persona row to edit; run migrations.")
        cur = await self.db.execute(
            "SELECT COALESCE(MAX(version), 0) FROM persona"
        )
        prev = (await cur.fetchone())[0]
        new_version = prev + 1
        voice = voice if voice is not None else current["voice"]
        principles = (
            principles if principles is not None else current["principles"]
        )
        mode_rules = (
            mode_rules if mode_rules is not None else current["mode_rules"]
        )
        now = datetime.now().isoformat()
        await self.db.execute(
            """INSERT INTO persona
               (version, voice, principles, mode_rules, active, created_at)
               VALUES (?, ?, ?, ?, 0, ?)""",
            (new_version, voice, principles, mode_rules, now),
        )
        await self.db.commit()
        return {
            "version": new_version,
            "voice": voice,
            "principles": principles,
            "mode_rules": mode_rules,
            "active": 0,
            "created_at": now,
        }

    async def set_persona_active(self, version: int) -> bool:
        """Activate the snapshot with `version` and deactivate all others
        (a pure flag flip — the rollback primitive). Returns False if the
        version does not exist."""
        if not await self.get_persona_snapshot(version):
            return False
        await self.db.execute("UPDATE persona SET active = 0")
        await self.db.execute(
            "UPDATE persona SET active = 1 WHERE version = ?", (version,)
        )
        await self.db.commit()
        return True

    # ─── Heartbeat ────────────────────────────────────────────────────────

    async def update_heartbeat(self) -> None:
        """Stamp the heartbeat row with the current time."""
        now = datetime.now().isoformat()
        await self.db.execute(
            """INSERT INTO heartbeat (id, last_seen) VALUES (1, ?)
               ON CONFLICT(id) DO UPDATE SET last_seen = excluded.last_seen""",
            (now,),
        )
        await self.db.commit()

    async def get_heartbeat(self) -> str | None:
        """Return the last heartbeat timestamp, or None if never stamped."""
        cursor = await self.db.execute("SELECT last_seen FROM heartbeat WHERE id = 1")
        row = await cursor.fetchone()
        return row["last_seen"] if row else None

    # ─── Feedback & corrections (Phase 2) ─────────────────────────────────

    async def add_correction(
        self,
        trigger: str,
        user_message: str | None = None,
        agent_action: str | None = None,
        correction: str | None = None,
    ) -> int:
        """Record a user correction (explicit / edit / thumbs_down)."""
        now = datetime.now().isoformat()
        cursor = await self.db.execute(
            """INSERT INTO corrections (created_at, trigger, user_message, agent_action, correction, consolidated)
               VALUES (?, ?, ?, ?, ?, 0)""",
            (now, trigger, user_message, agent_action, correction),
        )
        await self.db.commit()
        return cursor.lastrowid

    async def add_feedback(
        self,
        message_ref: str,
        rating: int,
        model_id: str | None = None,
        tier: str | None = None,
        note: str | None = None,
    ) -> int:
        """Record a 👍 (+1) / 👎 (-1) rating on an agent reply."""
        now = datetime.now().isoformat()
        cursor = await self.db.execute(
            """INSERT INTO feedback (created_at, message_ref, rating, model_id, tier, note)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (now, message_ref, rating, model_id, tier, note),
        )
        await self.db.commit()
        return cursor.lastrowid

    async def get_feedback_counts(self, days: int = 7) -> tuple[int, int]:
        """Return (thumbs_up, thumbs_down) counts for the last `days` days."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        cursor = await self.db.execute(
            """SELECT rating, COUNT(*) FROM feedback
               WHERE created_at >= ? GROUP BY rating""",
            (cutoff,),
        )
        rows = await cursor.fetchall()
        ups = downs = 0
        for row in rows:
            if int(row[0]) > 0:
                ups += int(row[1])
            else:
                downs += int(row[1])
        return ups, downs

    async def get_correction_counts(self, days: int = 7) -> int:
        """Return the number of corrections recorded in the last `days` days."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        cursor = await self.db.execute(
            "SELECT COUNT(*) FROM corrections WHERE created_at >= ?", (cutoff,)
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    # ─── Phase 7 — unified briefs & condition checks ─────────────────────

    async def get_active_tasks(self) -> list[Task]:
        """Every non-terminal task (pending or in_progress)."""
        cursor = await self.db.execute(
            "SELECT * FROM tasks WHERE status IN ('pending', 'in_progress')"
        )
        rows = await cursor.fetchall()
        return [Task(**dict(row)) for row in rows]

    async def get_overdue_active_tasks(self, cutoff_date: str) -> list[Task]:
        """Active tasks whose due date is strictly before `cutoff_date`
        (YYYY-MM-DD). Used by the brief and the condition pass (§7.4)."""
        cursor = await self.db.execute(
            """SELECT * FROM tasks
               WHERE status IN ('pending', 'in_progress')
                 AND due_date IS NOT NULL AND due_date < ?
               ORDER BY due_date ASC""",
            (cutoff_date,),
        )
        rows = await cursor.fetchall()
        return [Task(**dict(row)) for row in rows]

    async def get_active_notes_matching(
        self, fts_query: str, created_before: str, limit: int = 5
    ) -> list[Note]:
        """Notes that FTS-match an active-topic query and predate the cutoff
        (the 60+ day "unread" proxy in §7.4), ranked by bm25."""
        cursor = await self.db.execute(
            """SELECT n.* FROM notes n
               JOIN notes_fts fts ON n.id = fts.rowid
               WHERE notes_fts MATCH ? AND n.created_at < ?
               ORDER BY rank LIMIT ?""",
            (fts_query, created_before, limit),
        )
        rows = await cursor.fetchall()
        results = []
        for row in rows:
            data = dict(row)
            data["tags"] = json.loads(data["tags"]) if isinstance(data["tags"], str) else data["tags"]
            results.append(Note(**data))
        return results

    async def get_day_task_stats(
        self, day_start_iso: str, day_end_iso: str
    ) -> tuple[int, int]:
        """Return (tasks_created, tasks_completed) within a local day window
        (naive ISO boundaries, matching how timestamps are stored)."""
        cursor = await self.db.execute(
            "SELECT COUNT(*) FROM tasks WHERE created_at >= ? AND created_at < ?",
            (day_start_iso, day_end_iso),
        )
        created = int((await cursor.fetchone())[0])
        cursor = await self.db.execute(
            "SELECT COUNT(*) FROM tasks WHERE completed_at >= ? AND completed_at < ?",
            (day_start_iso, day_end_iso),
        )
        completed = int((await cursor.fetchone())[0])
        return created, completed

    async def record_nudge(
        self,
        entity_type: str,
        entity_id: int,
        condition: str,
        day: str | None = None,
        now_iso: str | None = None,
    ) -> bool:
        """Record a condition-check nudge.

        Returns True only when newly logged. The UNIQUE(day, entity_type,
        entity_id, condition) constraint makes a second fire the same day a
        no-op (§7.4: at most one nudge per entity per day).
        """
        day = day or datetime.now(Config.TIMEZONE).strftime("%Y-%m-%d")
        now_iso = now_iso or datetime.now().isoformat()
        cursor = await self.db.execute(
            """INSERT OR IGNORE INTO nudge_log
               (day, entity_type, entity_id, condition, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (day, entity_type, entity_id, condition, now_iso),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def count_nudges(
        self, entity_type: str, entity_id: int, since_day: str
    ) -> int:
        """Number of nudges recorded for an entity on or after `since_day`."""
        cursor = await self.db.execute(
            """SELECT COUNT(*) FROM nudge_log
               WHERE entity_type = ? AND entity_id = ? AND day >= ?""",
            (entity_type, entity_id, since_day),
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0
