"""SQLite database layer with async access and FTS5 full-text search."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import aiosqlite

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
        """Save a user preference or habit (upsert by key)."""
        now = datetime.now().isoformat()
        await self.db.execute(
            """INSERT INTO preferences (key, value, created_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = ?""",
            (pref.key, pref.value, now, pref.value),
        )
        await self.db.commit()

    async def get_all_preferences(self) -> list[Preference]:
        """Get all saved user preferences."""
        cursor = await self.db.execute("SELECT * FROM preferences ORDER BY created_at")
        rows = await cursor.fetchall()
        return [Preference(**dict(row)) for row in rows]

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
