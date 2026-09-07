"""Hermes-facing command-line bridge into the Second Brain.

Everything mutates/reads the exact same SQLite storage the Telegram bot
uses, through the exact same tool functions. This is what the Hermes
`second-brain` skill drives so the migration changes the front-end only:
zero data duplication, one source of truth.

Run from the project root (so `.env` and the default DATABASE_PATH resolve),
e.g.:  python -m secondbrain.cli tasks list --status all
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import timedelta

from agent import tools
from config import Config
from storage.database import Database


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="secondbrain",
        description="Second Brain standalone access via the existing tool layer.",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Optional explicit SQLite path (defaults to Config.DATABASE_PATH).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    tasks = sub.add_parser("tasks", help="Task operations")
    tasks_sub = tasks.add_subparsers(dest="action", required=True)
    add = tasks_sub.add_parser("add", help="Create a task")
    add.add_argument("description")
    add.add_argument("--priority", default="medium")
    add.add_argument("--due", dest="due_date", default=None)
    add.add_argument("--category", default="general")
    tasks_sub.add_parser("agenda", help="Today's agenda")
    lst = tasks_sub.add_parser("list", help="List tasks")
    lst.add_argument("--status", default="pending")
    lst.add_argument("--category", default=None)
    lst.add_argument("--date", default=None)
    comp = tasks_sub.add_parser("complete", help="Complete one or more tasks")
    comp.add_argument("ids", nargs="+")
    tasks_sub.add_parser("day-stats", help="Created vs completed today")

    notes = sub.add_parser("notes", help="Note operations")
    notes_sub = notes.add_subparsers(dest="action", required=True)
    save = notes_sub.add_parser("add", help="Save a note")
    save.add_argument("content")
    save.add_argument("--tags", default="[]")
    save.add_argument("--category", default="general")
    search = notes_sub.add_parser("search", help="Full-text search")
    search.add_argument("query")
    recent = notes_sub.add_parser("recent", help="Most recent notes")
    recent.add_argument("limit", nargs="?", type=int, default=10)

    reminders = sub.add_parser("reminders", help="Reminder operations")
    reminders_sub = reminders.add_subparsers(dest="action", required=True)
    set_rm = reminders_sub.add_parser("set", help="Set a reminder")
    set_rm.add_argument("message")
    set_rm.add_argument("--time", dest="trigger_time", required=True)
    set_rm.add_argument("--recurring", action="store_true")
    set_rm.add_argument("--cron", dest="cron_expression", default=None)
    rm_list = reminders_sub.add_parser("list", help="List reminders")
    rm_list.add_argument("--all", action="store_true", help="Include inactive")
    rm_list.add_argument("--query", default="")
    reminders_sub.add_parser("fire-due", help="Print + advance due reminders")

    prefs = sub.add_parser("prefs", help="Preferences")
    prefs_sub = prefs.add_subparsers(dest="action", required=True)
    save_pref = prefs_sub.add_parser("save", help="Save a preference")
    save_pref.add_argument("key")
    save_pref.add_argument("value")

    facts = sub.add_parser("facts", help="Durable facts about the user")
    facts_sub = facts.add_subparsers(dest="action", required=True)
    remember = facts_sub.add_parser("remember", help="Remember a fact")
    remember.add_argument("fact")
    remember.add_argument("--category", default="personal")
    remember.add_argument("--keywords", default="")

    corrections = sub.add_parser("corrections", help="Record corrections")
    corr_sub = corrections.add_subparsers(dest="action", required=True)
    corr_add = corr_sub.add_parser("add", help="Record a correction")
    corr_add.add_argument("correction")
    corr_add.add_argument("--scope", default="general")
    corr_sub.add_parser("list-today", help="Corrections recorded today")

    conditions = sub.add_parser("conditions", help="Condition checks (15-min pass)")
    conditions_sub = conditions.add_subparsers(dest="action", required=True)
    conditions_sub.add_parser("check", help="Evaluate and print condition nudges")

    maintenance = sub.add_parser("maintenance", help="Nightly DB maintenance")
    maintenance_sub = maintenance.add_subparsers(dest="action", required=True)
    maintenance_sub.add_parser("run", help="Retention + markdown export + backup")

    sub.add_parser("news", help="Fetch raw news for curation")
    sub.add_parser("agenda", help="Today's agenda")

    persona = sub.add_parser("persona", help="Persona layer management")
    persona_sub = persona.add_subparsers(dest="action", required=True)
    persona_sub.add_parser("show", help="Show the current persona")
    persona_sub.add_parser("history", help="Persona version history")
    ps = persona_sub.add_parser("set", help="Set a persona layer")
    ps.add_argument("layer", choices=["voice", "principles", "mode_rules"])
    ps.add_argument("text")
    pr = persona_sub.add_parser("rollback", help="Roll back persona")
    pr.add_argument("version", type=int)

    return parser


async def _run(command: str, args: argparse.Namespace) -> str:
    db_path = args.db or str(Config.DATABASE_PATH)
    db = Database(db_path)
    await db.connect()
    tools.set_database(db)
    try:
        if command == "tasks":
            return await _tasks(args, db)
        if command == "notes":
            return await _notes(args)
        if command == "reminders":
            return await _reminders(args)
        if command == "prefs":
            return await tools.save_preference(args.key, args.value)
        if command == "facts":
            return await tools.remember_fact(
                args.fact, category=args.category, keywords=args.keywords
            )
        if command == "corrections":
            return await _corrections(args, db)
        if command == "conditions":
            return await _conditions(args, db)
        if command == "maintenance":
            return await _maintenance(args, db)
        if command == "news":
            return await tools.get_news()
        if command == "agenda":
            return await tools.get_today_agenda()
        if command == "persona":
            return await _persona(args, db)
        return f"Unknown command: {command}"
    finally:
        await db.close()


async def _tasks(args: argparse.Namespace, db: Database) -> str:
    action = getattr(args, "action", "")
    if action == "add":
        return await tools.add_task(
            args.description,
            priority=getattr(args, "priority", "medium"),
            due_date=getattr(args, "due_date", None),
            category=getattr(args, "category", "general"),
        )
    if action == "agenda":
        return await tools.get_today_agenda()
    if action == "list":
        return await tools.list_tasks(
            status=getattr(args, "status", "pending"),
            category=getattr(args, "category", None),
            date=getattr(args, "date", None),
        )
    if action == "complete":
        return await tools.complete_tasks(getattr(args, "ids", []))
    if action == "day-stats":
        from datetime import datetime as dt

        now = dt.now(Config.TIMEZONE)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        created, completed = await db.get_day_task_stats(
            day_start.isoformat(), day_end.isoformat()
        )
        return f"Created today: {created} | Completed today: {completed}"
    return "Unknown tasks action"


async def _notes(args: argparse.Namespace) -> str:
    action = getattr(args, "action", "")
    if action == "add":
        return await tools.save_note(
            args.content,
            tags=getattr(args, "tags", "[]"),
            category=getattr(args, "category", "general"),
        )
    if action == "search":
        return await tools.search_notes(args.query)
    if action == "recent":
        return await tools.get_recent_notes(getattr(args, "limit", 10))
    return "Unknown notes action"


async def _reminders(args: argparse.Namespace) -> str:
    action = getattr(args, "action", "")
    if action == "set":
        recurring = getattr(args, "recurring", False)
        cron = getattr(args, "cron_expression", None)
        return await tools.set_reminder(
            args.message,
            trigger_time=args.trigger_time,
            is_recurring=recurring or bool(cron),
            cron_expression=cron,
        )
    if action == "list":
        return await tools.get_reminders(
            active_only=not getattr(args, "all", False),
            query=getattr(args, "query", ""),
        )
    if action == "fire-due":
        return await _fire_due_reminders(_get_db())
    return "Unknown reminders action"


async def _fire_due_reminders(db: Database) -> str:
    """Replicates the old scheduler's reminder_check: fires due reminders and
    advances recurring ones (non-recurring are deactivated). Prints exactly the
    messages so a `--no-agent` Hermes cron job can deliver them verbatim.
    """
    from datetime import datetime as dt

    from services.scheduler import _next_cron_occurrence

    now_naive = dt.now(Config.TIMEZONE).isoformat()
    due = await db.get_due_reminders(now_naive)
    lines: list[str] = []
    for reminder in due:
        lines.append(f"⏰ **Reminder:** {reminder.message}")
        if not reminder.is_recurring:
            await db.deactivate_reminder(reminder.id)
            continue
        next_time = _next_cron_occurrence(reminder.cron_expression) if reminder.cron_expression else None
        if next_time:
            await db.update_reminder_time(reminder.id, next_time)
        else:
            await db.deactivate_reminder(reminder.id)
    try:
        await db.update_heartbeat()
    except Exception:
        pass
    return "\n".join(lines)


async def _corrections(args: argparse.Namespace, db: Database) -> str:
    action = getattr(args, "action", "")
    if action == "add":
        return await tools.record_correction(
            args.correction, scope=getattr(args, "scope", "general")
        )
    if action == "list-today":
        from datetime import datetime as dt

        now = dt.now(Config.TIMEZONE).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        rows = await db.get_corrections_since(now.isoformat())
        if not rows:
            return "No corrections recorded today."
        lines = ["🛠️ **Corrections today:**"]
        for row in rows:
            text = (row.get("correction") or "")[:200]
            created = (row.get("created_at") or "")[:16]
            lines.append(f"- {created}: {text}")
        return "\n".join(lines)
    return "Unknown corrections action"


async def _conditions(args: argparse.Namespace, db: Database) -> str:
    from services.triggers import run_condition_checks

    lines = await run_condition_checks(db)
    if not lines:
        return ""
    body = "\n\n".join(lines)
    if len(lines) > 1:
        body = "🧠 **A few things need your attention:**\n\n" + body
    return body


async def _maintenance(args: argparse.Namespace, db: Database) -> str:
    from services.export import run_backup, run_markdown_export, run_retention

    await run_retention(db)
    await run_markdown_export(db)
    result = await run_backup(db)
    out = "🧹 Nightly maintenance completed (retention + export)."
    if result.get("skipped"):
        out += " Backup skipped (BACKUP_BUCKET unset)."
    elif result.get("uploaded"):
        out += f" Backup uploaded ({result.get('blob_name', '')})."
    return out


def _get_db() -> Database:
    from agent.tools import _get_db as undr

    return undr()


async def _persona(args: argparse.Namespace, db: Database) -> str:
    from services.persona_control import (
        persona_history,
        persona_rollback,
        persona_set,
        persona_show,
    )

    action = getattr(args, "action", "")
    if action == "show":
        return await persona_show(db)
    if action == "history":
        return await persona_history(db)
    if action == "set":
        return await persona_set(db, args.layer, args.text)
    if action == "rollback":
        return await persona_rollback(db, args.version)
    return "Unknown persona action"


def main() -> int:
    args = _build_parser().parse_args()
    try:
        result = asyncio.run(_run(args.command, args))
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())