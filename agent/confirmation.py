"""Confirmation gate for mutating tools.

When the assistant proposes a tool that changes state, the system asks the
user to confirm before executing it — like a real assistant. Read-only tools
run freely.
"""

from __future__ import annotations

import re

# Tools that change state and therefore require user confirmation.
CONFIRMING_TOOLS = {
    "add_task",
    "complete_task",
    "save_note",
    "set_reminder",
    "save_preference",
}

# How long a pending confirmation stays valid before it must be re-requested.
CONFIRMATION_TTL_SECONDS = 600

# A user reply beginning with any of these counts as confirmation.
_AFFIRMATIONS = (
    r"(?:yes\b|yep\b|yup\b|yeah\b|ya\b|y\b|sure\b|ok\b|okay\b|okey\b|okie\b|alright\b|"
    r"all right\b|right\b|fine\b|righto\b|got it\b|sounds good\b|good\b|lets? do it\b|"
    r"go ahead\b|do it\b|proceed\b|confirm(?:ed|ing|ation)?\b|approved\b)"
)
_AFFIRMATION_RE = re.compile(
    rf"^\s*(?:(?:please\s+)?{_AFFIRMATIONS}|\b{_AFFIRMATIONS}\b)",
    re.IGNORECASE,
)


def is_confirmation(text: str) -> bool:
    """True if the message is an affirmation of a pending action."""
    return bool(_AFFIRMATION_RE.match(text or ""))


def _clip(value: object, limit: int = 90) -> str:
    s = str(value or "").strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def confirmation_question(tool_name: str, args: dict | None) -> str | None:
    """Build the natural-language confirmation question for a tool call.

    Returns None when the tool needs no confirmation or args are unusable.
    """
    args = args or {}

    if tool_name == "add_task":
        desc = _clip(args.get("description", ""))
        if not desc:
            return None
        due = args.get("due_date")
        priority = args.get("priority")
        extra = []
        if priority and priority != "medium":
            extra.append(priority)
        if due:
            extra.append(f"due {due}")
        suffix = f" ({', '.join(extra)})" if extra else ""
        return f"Do you want me to add the task **{desc}**{suffix}?"

    if tool_name == "complete_task":
        task_id = args.get("task_id")
        if task_id is None:
            return None
        return f"Do you want me to mark task **#{task_id}** as completed?"

    if tool_name == "save_note":
        content = _clip(args.get("content", ""))
        if not content:
            return None
        return f"Do you want me to save this note: *{content}*?"

    if tool_name == "set_reminder":
        body = _clip(args.get("message", ""))
        when = args.get("trigger_time")
        if not body:
            return None
        when_text = f" at {when}" if when else ""
        return f"Do you want me to set a reminder{when_text}: *{body}*?"

    if tool_name == "save_preference":
        key = args.get("key")
        value = _clip(args.get("value", ""), 60)
        if not key or not value:
            return None
        return (
            f"Do you want me to remember this about you: "
            f"**{key}** = *{value}*?"
        )

    return None