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
            return await _tasks(args)
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
            return await tools.record_correction(
                args.correction, scope=args.scope
            )
        if command == "news":
            return await tools.get_news()
        if command == "agenda":
            return await tools.get_today_agenda()
        if command == "persona":
            return await _persona(args, db)
        return f"Unknown command: {command}"
    finally:
        await db.close()


async def _tasks(args: argparse.Namespace) -> str:
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
    return "Unknown reminders action"


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