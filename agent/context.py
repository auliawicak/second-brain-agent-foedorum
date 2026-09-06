"""Context assembly — the single place that enforces prompt size limits.

Used by every model call path. Hard caps:
- `MAX_CONTEXT_MESSAGES` exchanges of history
- `MAX_PREFS_INJECTED` preferences injected into the system block
- `MAX_PROMPT_CHARS` total assembled prompt (system block + history);
  overflow drops the OLDEST history first, never the system block.
"""

from __future__ import annotations

from config import Config


def build_system_prompt(
    core: str,
    *,
    sections: list[str] | None = None,
) -> str:
    """Assemble the system prompt. `sections` are appended in order.

    The assembled block is hard-capped at `MAX_PROMPT_CHARS` so the persona
    itself can never overflow the prompt budget (Phase 3 §3.4 'persona
    assembly is capped'). Trailing sections are dropped first; the core
    persona is always kept.
    """
    parts = [core]
    if sections:
        parts.extend(s for s in sections if s)
    prompt = "\n".join(parts)
    if len(prompt) <= Config.MAX_PROMPT_CHARS:
        return prompt

    # Drop trailing sections until the core (habitually the largest block)
    # plus a single remaining section fits.
    budget = Config.MAX_PROMPT_CHARS - len(core) - 2  # "\n" separators
    if budget <= 0:
        return core[: Config.MAX_PROMPT_CHARS]
    kept = [core]
    used = len(core)
    for s in (sections or []):
        if not s:
            continue
        if used + len(s) + 2 > Config.MAX_PROMPT_CHARS:
            break
        kept.append(s)
        used += len(s) + 2
    return "\n".join(kept)


def cap_preferences(prefs_texts: list[str]) -> list[str]:
    """Return at most `MAX_PREFS_INJECTED` preference lines."""
    if len(prefs_texts) <= Config.MAX_PREFS_INJECTED:
        return prefs_texts
    return prefs_texts[: Config.MAX_PREFS_INJECTED]


def build_context(
    history: list[dict],
    system_prompt: str,
) -> list[dict]:
    """Trim an assembled history to fit the model context window.

    Args:
        history: Conversation messages (role/content dicts), oldest first.
        system_prompt: The fully-assembled system block (protected from trim).

    Returns:
        The messages to send, capped at `MAX_CONTEXT_MESSAGES` exchanges
        and truncated to fit `MAX_PROMPT_CHARS` (dropping oldest messages).
    """
    max_msgs = Config.MAX_CONTEXT_MESSAGES * 2  # user + assistant per exchange
    if len(history) > max_msgs:
        history = history[-max_msgs:]

    system_len = len(system_prompt)
    budget = Config.MAX_PROMPT_CHARS - system_len
    if budget <= 0:
        return []

    total = sum(len(m.get("content", "")) + 32 for m in history)
    if total <= budget:
        return history

    # Drop oldest messages first (iterate newest-first, stop when full).
    # The newest message is always kept — truncated to the remaining budget
    # if needed so the latest context is never lost wholesale.
    kept: list[dict] = []
    remaining = budget
    for i, msg in enumerate(reversed(history)):
        size = len(msg.get("content", "")) + 32
        if remaining - size >= 0:
            kept.append(msg)
            remaining -= size
            continue
        if i == 0:
            avail = max(remaining - 32, 0)
            if avail > 0:
                kept.append(
                    {
                        "role": msg.get("role", "user"),
                        "content": msg.get("content", "")[:avail],
                    }
                )
            remaining = 0
            break
        break
    return list(reversed(kept))