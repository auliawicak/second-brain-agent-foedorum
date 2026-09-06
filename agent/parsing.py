"""Body parsing and recovery for model outputs (Phase 1).

Tool outputs and structured replies arrive as text that *should* be JSON but
often carries markdown fences, prose, or minor syntax damage. We strip fences,
locate the first complete JSON object, and retry a single targeted repair
(fixup of trailing commas / single quotes) before giving up.
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)


def _extract_raw_json(text: str) -> str | None:
    """Strip fences + prose and return the first complete JSON object/array."""
    # Pattern 1: fenced code block (```json on its own line).
    fence = re.search(r"```(?:json)?\s*\n(.*?)\n\s*```", text, re.DOTALL)
    if fence:
        return fence.group(1).strip()

    # General case: find the first { or [, walk balanced braces, and return
    # that slice. Handles inline fences (```json {"tool":…}``` on one line),
    # prose-then-JSON, and trailing commentary after the object.
    idx = -1
    for i, ch in enumerate(text):
        if ch in "{[":     # also catch `"{"` inside code-fence-on-same-line
            idx = i
            break
    if idx == -1:
        return None
    open_ch = text[idx]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    for i in range(idx, len(text)):
        if text[i] == open_ch:
            depth += 1
        elif text[i] == close_ch:
            depth -= 1
            if depth == 0:
                return text[idx : i + 1].strip()
    return None


def _repair(raw: str) -> str:
    """Point repairs for the most common JSON breaks a model produces."""
    # Trim leading prose up to the first brace/bracket.
    repaired = raw
    # Trailing commas before } or ].
    repaired = re.sub(r",(\s*[}\]])", r"\1", repaired)
    # Lone single quotes used instead of double (only at value/string edges).
    repaired = re.sub(r"'", '"', repaired)
    return repaired


def looks_like_tool_response(text: str) -> bool:
    """Heuristic: the model *attempted* a tool call but it didn't parse."""
    if '"tool"' in text or "'tool'" in text or "```json" in text:
        return True
    brace = text.find("{")
    return brace != -1 and ("\n" in text[: max(brace, 0)] or "{" in text[:brace])


def parse_tool_call(response_text: str) -> tuple[str, dict] | None:
    """Return (tool_name, args) if the text encodes a tool call, else None.

    Attempts normal parse, then a single repaired parse. Returns None when
    there is genuinely no recoverable tool invocation.
    """
    raw = _extract_raw_json(response_text)
    if not raw:
        return None

    for candidate in (raw, _repair(raw)):
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        name = obj.get("tool")
        if not name:
            return None
        args = obj.get("args")
        if not isinstance(args, dict):
            args = {}
        return str(name), args
    return None


def extract_json_object(text: str) -> dict | None:
    """Return the first complete JSON object in `text`, or None.

    Tolerant of fences/prose and of the usual repair targets. Used by the
    correction classifier and other small structured replies.
    """
    raw = _extract_raw_json(text)
    if not raw:
        return None
    for candidate in (raw, _repair(raw)):
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None