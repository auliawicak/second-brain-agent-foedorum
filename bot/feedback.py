"""Pure helpers for Phase 2 feedback capture.

Kept dependency-free (no httpx/telegram) so they are trivially unit-testable.
"""

from __future__ import annotations

import re

# A reply is "non-trivial" (gets 👍/👎 buttons) when it executed a tool or is
# long enough to be worth a quick judgement call.
NONTRIVIAL_LENGTH = 200

_CB_PREFIX = "fb"
_CB_SEP = "|"


def should_attach_feedback(text: str, tool_executed: bool) -> bool:
    """True when a reply is non-trivial: it ran a tool or exceeded 200 chars."""
    return bool(tool_executed) or len(text or "") > NONTRIVIAL_LENGTH


def encode_feedback_cb(rating: int, token: str) -> str:
    """Encode callback_data for a 👍/👎 button.

    The callback message itself carries chat_id/message_id, so the button only
    needs to ship the rating and a lookup token (kept well under the 64-byte
    callback_data limit).
    """
    return f"{_CB_PREFIX}{_CB_SEP}{rating}{_CB_SEP}{token}"


def decode_feedback_cb(data: str) -> dict | None:
    """Decode callback_data; returns {'rating': ±1, 'token': str} or None."""
    parts = data.split(_CB_SEP)
    if len(parts) != 3 or parts[0] != _CB_PREFIX:
        return None
    if parts[1] not in ("1", "-1"):
        return None
    token = parts[2]
    if not token:
        return None
    return {"rating": int(parts[1]), "token": token}


def parse_created_id(text: str | None) -> int | None:
    """Extract the entity id the agent just created (e.g. 'Task #12')."""
    if not text:
        return None
    m = re.search(r"#(\d+)", text)
    return int(m.group(1)) if m else None


def detect_edit_request(text: str) -> int | None:
    """Return a task/note id the user is modifying/deleting, or None.

    Conservative: requires an explicit modify/complete/delete verb plus an id
    reference, so ordinary chatter doesn't produce false 'edit' corrections.
    """
    t = (text or "").lower()
    verbs = ("delete", "remove", "complete", "done", "cancel", "finish", "modify", "edit")
    if not any(v in t for v in verbs):
        return None
    m = re.search(r"(?:task\s*)?#?(\d+)", t)
    if not m:
        return None
    return int(m.group(1))