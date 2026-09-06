"""Condition-check nudges (Phase 7 §7.4).

One job every 15 minutes runs the *indexed* trigger conditions that operate
on existing data (tasks/notes). The account/touchpoint conditions from the
spec are intentionally absent: they belong to the Phase 4 data model, which
has not landed in this repo yet.

Guarantees:
- Deterministic and model-free — no LLM calls, just a few indexed queries,
  so the whole pass stays well under 2s even at 1,000+ tasks.
- Idempotent per day: every firing entity is recorded in `nudge_log`; the
  UNIQUE(day, entity_type, entity_id, condition) constraint means no
  condition fires twice for the same entity in one day.
- Bounded output: at most a handful of nudge lines per run.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from config import Config
from storage.database import Database, keyword_query

logger = logging.getLogger(__name__)

OVERDUE_HOURS = 24          # task due >24h ago and still active counts
IMBALANCE_CREATED = 5       # more than 5 tasks captured today...
IMBALANCE_COMPLETED = 0     # ...and none completed → imbalance flag
NOTE_STALE_DAYS = 60        # note unread proxy: 60+ days since saved
MAX_OVERDUE_NUDGES = 3
MAX_NOTE_NUDGES = 3
MAX_TOPIC_TERMS = 6


async def run_condition_checks(db: Database) -> list[str]:
    """Evaluate the trigger conditions and return the nudge lines to send.

    A second call on the same local day returns no repeats (guarded by
    `nudge_log`). Returns an empty list when everything is quiet.
    """
    now = datetime.now(Config.TIMEZONE)
    day = now.strftime("%Y-%m-%d")
    now_naive = now.replace(tzinfo=None)
    lines: list[str] = []

    # 1. Task overdue >24h and untouched → ask: drop / reschedule / delegate?
    cutoff_date = (now_naive - timedelta(hours=OVERDUE_HOURS)).strftime("%Y-%m-%d")
    for task in (await db.get_overdue_active_tasks(cutoff_date))[:MAX_OVERDUE_NUDGES]:
        if await db.record_nudge("task", task.id, "overdue", day=day):
            lines.append(
                f"⏳ *#{task.id} {task.description}* was due {task.due_date} "
                "(overdue) and hasn't been touched. Drop it, reschedule it, or delegate it?"
            )

    # 2. Capture/execute imbalance: >5 captured today, none completed.
    start = now_naive.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    created, completed = await db.get_day_task_stats(start.isoformat(), end.isoformat())
    if created > IMBALANCE_CREATED and completed <= IMBALANCE_COMPLETED:
        if await db.record_nudge("day", now.date().toordinal(), "imbalance", day=day):
            lines.append(
                f"⚖️ You've captured {created} tasks today but completed none. "
                "Finish a couple before adding more?"
            )

    # 3. Note unread 60+ days matching an active topic → surface it.
    lines.extend(await _stale_note_lines(db, now_naive, day))

    return lines


async def _stale_note_lines(db: Database, now_naive: datetime, day: str) -> list[str]:
    terms = await _active_topic_terms(db)
    query = keyword_query(" ".join(terms))
    if not query:
        return []

    created_before = (now_naive - timedelta(days=NOTE_STALE_DAYS)).isoformat()
    since_day = (now_naive - timedelta(days=NOTE_STALE_DAYS)).strftime("%Y-%m-%d")
    notes = await db.get_active_notes_matching(
        query, created_before, limit=MAX_NOTE_NUDGES + 2
    )

    lines: list[str] = []
    for note in notes:
        if await db.count_nudges("note", note.id, since_day) > 0:
            continue  # surfaced recently — effectively read, don't nag
        if not await db.record_nudge("note", note.id, "stale", day=day):
            continue
        snippet = (note.content or "").replace("\n", " ")[:140]
        saved = (note.created_at or "")[:10]
        topics = " | ".join(terms[:3])
        lines.append(
            f"📌 You saved a note {saved} you haven't looked at: *{snippet}* "
            f"(topic: {topics}) — worth a re-read while it's relevant?"
        )
        if len(lines) >= MAX_NOTE_NUDGES:
            break
    return lines


async def _active_topic_terms(db: Database) -> list[str]:
    """Distinctive tokens from active (pending/in_progress) tasks."""
    tasks = await db.get_active_tasks()
    terms: set[str] = set()
    for t in tasks:
        for tok in re.findall(
            r"[a-z0-9']{4,}", f"{t.description} {t.category}".lower()
        ):
            if tok in _TOPIC_STOPWORDS:
                continue
            terms.add(tok)
            if len(terms) >= MAX_TOPIC_TERMS:
                return sorted(terms)
    return sorted(terms)


_TOPIC_STOPWORDS = {
    "about", "after", "again", "also", "before", "been", "general", "going",
    "have", "into", "need", "over", "personal", "please", "should", "sure",
    "task", "tasks", "that", "them", "then", "there", "these", "this",
    "tomorrow", "until", "when", "will", "with", "work", "would", "your",
}


__all__ = ["run_condition_checks"]