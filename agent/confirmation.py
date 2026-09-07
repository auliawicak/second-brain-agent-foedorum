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
    "complete_tasks",
    "save_note",
    "set_reminder",
    "save_preference",
    "remember_fact",
    "record_correction",
}

# How long a pending confirmation stays valid before it must be re-requested.
# Deliberately generous: the user often replies much later than the last model
# question. An expired confirmation is never silently swallowed — it is
# re-asked explicitly so the user's "yes" always lands on a live action.
CONFIRMATION_TTL_SECONDS = 3600

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


# A user reply beginning with any of these declines a pending action.
_REJECTIONS = (
    r"(?:no\b|nope\b|nah\b|cancel\b|never ?mind\b|forget it\b|skip\b|"
    r"decline\b|dont\b|stop\b|drop it\b|leave it\b|not now\b)"
)
_REJECTION_RE = re.compile(rf"^\s*{_REJECTIONS}", re.IGNORECASE)


def is_rejection(text: str) -> bool:
    """True if the message declines a pending action (e.g. 'no', 'cancel')."""
    t = (text or "").strip().lower().replace("'", "").replace("`", "")
    return bool(_REJECTION_RE.match(t))


def _clip(value: object, limit: int = 90) -> str:
    s = str(value or "").strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _pretty_ids(values: object) -> str:
    """Coerce a mixed list of ids (ints or '3'/'#3' strings) into '1, 2, and 7'.

    Invalid entries are ignored; duplicates collapsed in first-seen order.
    """
    seen: list[int] = []
    for value in values or []:
        try:
            n = int(str(value).strip().lstrip("#"))
        except (TypeError, ValueError):
            continue
        if n > 0 and n not in seen:
            seen.append(n)
    if not seen:
        return ""
    if len(seen) == 1:
        return str(seen[0])
    head = ", ".join(str(n) for n in seen[:-1])
    return f"{head}, and {seen[-1]}"


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

    if tool_name == "complete_tasks":
        pretty = _pretty_ids(args.get("task_ids") or [])
        if not pretty:
            return None
        if "," in pretty:
            return f"Do you want me to mark tasks **#{pretty}** as completed?"
        return f"Do you want me to mark task **#{pretty}** as completed?"

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

    if tool_name == "remember_fact":
        fact = _clip(args.get("fact", ""), 90)
        if not fact:
            return None
        return f"Do you want me to remember this about you: *{fact}*?"

    if tool_name == "record_correction":
        correction = _clip(args.get("correction", ""), 90)
        if not correction:
            return None
        return (
            f"Shall I record this so I do better next time: *{correction}*?"
        )

    return None


def summarize_action(tool_name: str, args: dict | None) -> str:
    """Human-readable summary of a pending action (for expiry/pending hints)."""
    return confirmation_question(tool_name, args) or f"run `{tool_name}`"