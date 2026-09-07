"""Custom tools exposed to the AI agent for database and service access."""

from __future__ import annotations

import json
import logging
from datetime import datetime

from storage.database import Database
from storage.models import NoteCreate, PreferenceCreate, ReminderCreate, TaskCreate, TaskPriority, TaskStatus

logger = logging.getLogger(__name__)

# The database instance is injected at startup via set_database()
_db: Database | None = None


def set_database(database: Database) -> None:
    """Inject the database instance for tools to use."""
    global _db
    _db = database


def _get_db() -> Database:
    if _db is None:
        raise RuntimeError("Database not initialized. Call set_database() first.")
    return _db


# ─── Task Tools ───────────────────────────────────────────────────────────────


async def add_task(
    description: str,
    priority: str = "medium",
    due_date: str | None = None,
    category: str = "general",
) -> str:
    """Create a new task in the user's task list.

    Args:
        description: What needs to be done.
        priority: Priority level — one of 'low', 'medium', 'high', 'urgent'.
        due_date: Optional due date in YYYY-MM-DD format.
        category: Category like 'work', 'personal', 'health', 'learning'.
    """
    db = _get_db()
    task_data = TaskCreate(
        description=description,
        priority=TaskPriority(priority),
        due_date=due_date,
        category=category,
    )
    task = await db.add_task(task_data)
    return f"✅ Task #{task.id} created: {task.description} [{task.priority.value}]"


async def list_tasks(
    status: str = "pending",
    category: str | None = None,
    date: str | None = None,
) -> str:
    """List tasks with optional filters.

    Args:
        status: Filter by status — 'pending', 'in_progress', 'done', 'archived'. Use 'all' for no filter.
        category: Optional category filter like 'work', 'personal'.
        date: Optional date filter in YYYY-MM-DD format.
    """
    db = _get_db()
    task_status = TaskStatus(status) if status != "all" else None
    tasks = await db.get_tasks(status=task_status, date=date, category=category)

    if not tasks:
        return "No tasks found matching the criteria."

    lines = []
    for t in tasks:
        status_icon = {"pending": "⬜", "in_progress": "🔄", "done": "✅", "archived": "📦"}.get(
            t.status.value, "❓"
        )
        priority_icon = {"urgent": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}.get(
            t.priority.value, ""
        )
        due = f" (due: {t.due_date})" if t.due_date else ""
        lines.append(f"{status_icon} #{t.id} {priority_icon} {t.description}{due} [{t.category}]")

    return "\n".join(lines)


async def complete_task(task_id: int) -> str:
    """Mark a task as completed.

    Args:
        task_id: The numeric ID of the task to complete.
    """
    db = _get_db()
    task = await db.complete_task(int(task_id))
    if task:
        return f"✅ Task #{task.id} completed: {task.description}"
    return f"❌ Task #{task_id} not found."


async def complete_tasks(task_ids: list) -> str:
    """Mark several tasks as completed in a single action.

    Prefer this over complete_task when the user asks to finish multiple tasks
    at once — the user only confirms one batched action.

    Args:
        task_ids: The IDs of the tasks to complete. Accepts ints or '#id'/'id' strings.
    """
    db = _get_db()
    seen: list[int] = []
    for value in task_ids or []:
        try:
            n = int(str(value).strip().lstrip("#"))
        except (TypeError, ValueError):
            continue
        if n > 0 and n not in seen:
            seen.append(n)
    if not seen:
        return "❌ No valid task IDs given."

    lines = []
    for n in seen:
        task = await db.complete_task(n)
        if task:
            lines.append(f"✅ Task #{task.id} completed: {task.description}")
    if not lines:
        return "❌ None of those tasks were found."
    return "\n".join(lines)


async def get_today_agenda() -> str:
    """Get today's agenda: pending tasks, due items, and active reminders."""
    db = _get_db()
    today = datetime.now().strftime("%Y-%m-%d")
    tasks = await db.get_today_tasks(today)
    reminders = await db.get_active_reminders()

    lines = [f"📅 **Agenda for {today}**\n"]

    if tasks:
        lines.append("**Tasks:**")
        for t in tasks:
            priority_icon = {"urgent": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}.get(
                t.priority.value, ""
            )
            due = f" (due: {t.due_date})" if t.due_date else " (no due date)"
            lines.append(f"  {priority_icon} #{t.id} {t.description}{due}")
    else:
        lines.append("**Tasks:** No pending tasks! 🎉")

    if reminders:
        lines.append("\n**Reminders:**")
        for r in reminders:
            lines.append(f"  ⏰ {r.message} — {r.trigger_time}")
    else:
        lines.append("\n**Reminders:** None active.")

    return "\n".join(lines)


# ─── Note Tools ───────────────────────────────────────────────────────────────


async def save_note(
    content: str,
    tags: str = "[]",
    category: str = "general",
) -> str:
    """Save a note, idea, or piece of information to the user's second brain.

    Args:
        content: The note content to save.
        tags: JSON array of tags, e.g. '["idea", "project-x"]'.
        category: Category like 'work', 'personal', 'research', 'idea'.
    """
    db = _get_db()
    parsed_tags = json.loads(tags) if isinstance(tags, str) else tags
    note_data = NoteCreate(content=content, tags=parsed_tags, category=category)
    note = await db.add_note(note_data)
    tag_str = ", ".join(f"#{t}" for t in note.tags) if note.tags else ""
    return f"📝 Note #{note.id} saved.{' Tags: ' + tag_str if tag_str else ''}"


async def search_notes(query: str) -> str:
    """Search through saved notes using full-text search.

    Args:
        query: The search query to find relevant notes.
    """
    db = _get_db()
    notes = await db.search_notes(query)

    if not notes:
        return f"No notes found matching '{query}'."

    lines = [f"🔍 Found {len(notes)} note(s) for '{query}':\n"]
    for n in notes:
        tag_str = " ".join(f"#{t}" for t in n.tags) if n.tags else ""
        lines.append(f"📝 #{n.id} ({n.created_at[:10]}): {n.content[:200]}{'...' if len(n.content) > 200 else ''}")
        if tag_str:
            lines.append(f"   Tags: {tag_str}")
    return "\n".join(lines)


async def get_recent_notes(limit: int = 10) -> str:
    """Get the most recent saved notes.

    Args:
        limit: Maximum number of notes to return (default 10).
    """
    db = _get_db()
    notes = await db.get_recent_notes(int(limit))

    if not notes:
        return "No notes saved yet."

    lines = ["📒 **Recent Notes:**\n"]
    for n in notes:
        tag_str = " ".join(f"#{t}" for t in n.tags) if n.tags else ""
        lines.append(f"📝 #{n.id} ({n.created_at[:10]}): {n.content[:150]}{'...' if len(n.content) > 150 else ''}")
        if tag_str:
            lines.append(f"   Tags: {tag_str}")
    return "\n".join(lines)


# ─── Reminder Tools ──────────────────────────────────────────────────────────


async def set_reminder(
    message: str,
    trigger_time: str,
    is_recurring: bool = False,
    cron_expression: str | None = None,
) -> str:
    """Set a reminder that will be sent at a specific time.

    Args:
        message: The reminder message to send.
        trigger_time: When to trigger, in ISO datetime format (YYYY-MM-DDTHH:MM:SS).
        is_recurring: Whether this reminder repeats.
        cron_expression: Cron expression for recurring reminders (e.g. '0 9 * * 1-5').
    """
    db = _get_db()
    reminder_data = ReminderCreate(
        message=message,
        trigger_time=trigger_time,
        is_recurring=is_recurring,
        cron_expression=cron_expression,
    )
    reminder = await db.add_reminder(reminder_data)
    recur_text = f" (recurring: {cron_expression})" if is_recurring else ""
    return f"⏰ Reminder #{reminder.id} set for {trigger_time}{recur_text}: {message}"


async def get_reminders(active_only: bool = True, query: str = "") -> str:
    """Look up the reminders that are set up.

    Use this whenever the user asks whether a reminder exists ('do I still
    have my X reminder?'), what reminders are set, or what will fire next —
    THEN answer them directly with the result.

    Args:
        active_only: If True (default) return only active reminders; if False
            include inactive/expired ones too.
        query: Optional substring filter on the reminder message (case-insensitive).
            Pass '' to list everything.
    """
    db = _get_db()
    reminders = (
        await db.get_all_reminders()
        if not active_only
        else await db.get_active_reminders()
    )
    if query:
        q = query.strip().lower()
        reminders = [r for r in reminders if q in r.message.lower()]

    if not reminders:
        if query:
            return f"No reminders match '{query}'."
        return "No reminders are set up."

    lines = [f"⏰ {'All' if not active_only else 'Active'} reminders ({len(reminders)}):"]
    for r in reminders:
        recur = f" (recurring: {r.cron_expression})" if r.is_recurring else ""
        state = "" if r.is_active else " [inactive]"
        lines.append(f"• #{r.id} {r.message} — {r.trigger_time}{recur}{state}")
    return "\n".join(lines)


# ─── Utility Tools ────────────────────────────────────────────────────────────


def get_current_datetime() -> str:
    """Get the current date and time in the user's timezone (Asia/Jakarta, UTC+7).

    Returns the current date, time, and day of week.
    """
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo("Asia/Jakarta"))
    return now.strftime("Current date/time: %A, %B %d, %Y at %H:%M:%S (UTC+7)")


async def save_preference(key: str, value: str) -> str:
    """Save a user habit, preference, or fact so you remember it in the future.

    Args:
        key: A short identifier for the preference (e.g. 'morning_drink', 'workout_time', 'diet').
        value: The actual detail (e.g. 'Prefers black coffee', 'Works out at 6am', 'Vegan').
    """
    db = _get_db()
    pref_data = PreferenceCreate(key=key, value=value)
    await db.save_preference(pref_data)
    return f"🧠 Learned and saved preference: {key} = {value}"


# ─── Learning-Loop Tools (Phase 6 §6.2) ──────────────────────────────────────


async def remember_fact(
    fact: str,
    category: str = "personal",
    keywords: str = "",
) -> str:
    """Remember a durable preference, habit, or recurring fact about the user.

    Use this whenever the user shares a preference, habit, or standing fact
    that matters beyond this conversation. The fact is stored with the
    learning loop: restatements strengthen it, contradictions supersede it.

    Args:
        fact: The preference as a full statement (e.g. 'Prefers black coffee in the morning').
        category: One of 'personal', 'diet', 'work', 'health', 'home', 'social', 'finance', 'travel'.
        keywords: Optional space-separated retrieval keywords (e.g. 'coffee morning drink').
    """
    db = _get_db()
    outcome, pid = await db.merge_fact(
        fact=fact,
        category=category,
        keywords=keywords,
        confidence=0.7,
        evidence_ref="assistant:remember_fact",
    )
    label = {
        "new": "Learned",
        "matched": "Refreshed (I already knew this)",
        "superseded": "Revised",
    }.get(outcome, "Saved")
    return f"🧠 {label} preference #{pid}: {fact}"


async def record_correction(
    correction: str,
    scope: str = "general",
) -> str:
    """Record a user correction about how to do things, so future turns improve.

    Use this when the user tells you how to do something differently, points
    out a wrong assumption about them, or corrects something you did. The
    nightly consolidation turns accumulations of these into stable preferences.

    Args:
        correction: What the user wants done differently (e.g. 'Always schedule my workouts for the morning').
        scope: Which area it applies to: 'general', 'tasks', 'notes', 'reminders', 'responses', 'preferences'.
    """
    db = _get_db()
    cid = await db.add_correction(
        trigger="explicit",
        user_message=correction,
        correction=correction,
    )
    return f"🛠️ Recorded correction #{cid} (scope: {scope}). I'll apply this going forward and consolidate it tonight."


async def get_news() -> str:
    """Fetch and curate the latest top news stories.

    Returns a curated digest of the most important recent news.
    """
    from services.news import fetch_all_news

    raw_articles = await fetch_all_news()
    if "No news articles available" in raw_articles:
        return "No news available right now."

    return f"Here are the raw latest news articles. Please curate them for the user:\n\n{raw_articles}"


# ─── Tool Registry ───────────────────────────────────────────────────────────


ALL_TOOLS = [
    add_task,
    list_tasks,
    complete_task,
    complete_tasks,
    get_today_agenda,
    save_note,
    search_notes,
    get_recent_notes,
    set_reminder,
    get_reminders,
    get_current_datetime,
    save_preference,
    remember_fact,
    record_correction,
    get_news,
]
