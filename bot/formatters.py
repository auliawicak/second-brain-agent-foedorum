"""Telegram message formatting utilities.

Handles message truncation, pagination, and rich formatting for
Telegram's MarkdownV2 and HTML parse modes.
"""

from __future__ import annotations

import re

# Telegram's max message length
MAX_MESSAGE_LENGTH = 4096


def escape_markdown_v2(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2.

    Preserves intentional formatting (bold, italic, code) while
    escaping characters that would break the parser.
    """
    # Characters that need escaping in MarkdownV2
    special_chars = r"_[]()~`>#+=|{}.!-"
    result = ""
    i = 0
    while i < len(text):
        char = text[i]
        # Preserve bold markers **
        if char == "*" and i + 1 < len(text) and text[i + 1] == "*":
            result += "**"
            i += 2
            continue
        # Preserve code markers `
        if char == "`":
            result += "`"
            i += 1
            continue
        # Escape special characters
        if char in special_chars:
            result += f"\\{char}"
        else:
            result += char
        i += 1
    return result


def truncate_message(text: str, max_length: int = MAX_MESSAGE_LENGTH) -> str:
    """Truncate a message to fit within Telegram's limit.

    Tries to break at a sentence or paragraph boundary.
    """
    if len(text) <= max_length:
        return text

    # Leave room for the truncation indicator
    cutoff = max_length - 50

    # Try to break at a paragraph
    last_para = text.rfind("\n\n", 0, cutoff)
    if last_para > cutoff // 2:
        return text[:last_para] + "\n\n... _(message truncated)_"

    # Try to break at a sentence
    last_sentence = max(
        text.rfind(". ", 0, cutoff),
        text.rfind("! ", 0, cutoff),
        text.rfind("? ", 0, cutoff),
    )
    if last_sentence > cutoff // 2:
        return text[: last_sentence + 1] + "\n\n... _(message truncated)_"

    # Hard truncate
    return text[:cutoff] + "\n\n... _(message truncated)_"


def split_long_message(text: str, max_length: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Split a long message into multiple parts for sequential sending.

    Tries to split at paragraph boundaries.
    """
    if len(text) <= max_length:
        return [text]

    parts: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= max_length:
            parts.append(remaining)
            break

        # Find a good split point
        cutoff = max_length - 20
        split_at = remaining.rfind("\n\n", 0, cutoff)

        if split_at < cutoff // 3:
            split_at = remaining.rfind("\n", 0, cutoff)

        if split_at < cutoff // 3:
            split_at = cutoff

        parts.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()

    return parts


def format_task_list(tasks: list[dict]) -> str:
    """Format a list of tasks for Telegram display."""
    if not tasks:
        return "📋 No tasks found."

    lines = ["📋 **Your Tasks:**\n"]
    for task in tasks:
        status_icon = {
            "pending": "⬜",
            "in_progress": "🔄",
            "done": "✅",
            "archived": "📦",
        }.get(task.get("status", ""), "❓")

        priority_icon = {
            "urgent": "🔴",
            "high": "🟠",
            "medium": "🟡",
            "low": "🟢",
        }.get(task.get("priority", ""), "")

        due = f" (due: {task['due_date']})" if task.get("due_date") else ""
        lines.append(f"{status_icon} #{task['id']} {priority_icon} {task['description']}{due}")

    return "\n".join(lines)


def format_help_message() -> str:
    """Generate the help/command reference message."""
    return """🧠 **Second Brain — Command Reference**

**📌 Task Management:**
• `/addtask <description>` — Create a new task
• `/tasks` — View pending tasks
• `/done <id>` — Complete a task
• `/daily` — Today's agenda

**📝 Notes & Memory:**
• `/save <content>` — Save a note or idea
• `/notes` — View recent notes
• `/search <query>` — Search your notes

**📰 News:**
• `/news` — Get the latest curated news digest

**⏰ Reminders:**
• `/remind <time> <message>` — Set a reminder
  Example: `/remind 2025-12-31T09:00 New Year meeting`

**🧠 Deep Thinking:**
• `/think <prompt>` — Use the deep reasoning model

**💬 Free Chat:**
Just send any message and I'll respond as your assistant!

**ℹ️ System:**
• `/help` — Show this help message
• `/status` — System status
• `/persona show|history|set|rollback` — Persona as data
"""
