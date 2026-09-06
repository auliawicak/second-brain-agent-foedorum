"""Nightly preference-loop consolidation (Phase 6 §6.3).

Closed loop:
- Every flagged user correction from the day (`corrections` rows with
  consolidated=0) is fed to the `deep` tier in one bounded batch.
- The model returns discrete, bounded *statements* (with a section, an
  evidence reason, and one-hot flags). Nothing executes beyond the PMID
  schema — the loop stays deterministic and testable.
- Statements are applied straight into the preference store via
  `merge_fact`:
    * corrected-and-reinforced → evidence bump (matched)
    * a verbatim restatement → evidence bump (matched)
    * a contradiction of a live preference → **supersede** (never overwrite):
      the old row is kept, pointed at the new row
    * cold/long-lost pattern (evidence < 2, confidence < threshold over a
      long window) → **forget**: confidence floor, excluded from retrieval
    * high-confidence coachable rule → stored as an operating principle,
      unified with the blog-generation preference (the reason `is_principle`
      merges into the persona pipeline of §6.5)
- The digest is delivered to the owner as a single proposal (blockquote +
  quotable caption + Approve/Reject), and can be quoted back to start a
  conversation about any of it.

Bounded by design: MAX_INPUT_CHARS input, small output budget, TTL-equivalent
run limits, and hard content caps set in services.messaging.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from config import Config
from services.messaging import (
    MAX_CONSOLIDATION_SECTIONS,
    MAX_QUOTE_CHARS,
    MAX_STATEMENT_CHARS,
    send_proposal,
)
from storage.database import Database

logger = logging.getLogger(__name__)

ACTING_SECTIONS = {"preferences", "principles"}
MAX_INPUT_CHARS = 6000
MAX_OUTPUT_LINES = 400
SUPERSEDE_FORGET_THRESHOLD = 0.1
QUALIFY_CONFIDENCE = 0.6  # minimum to be stored as a fact (vs. dropped)
RUN_CAP = 15  # episodes processed per nightly run


async def consolidate_nightly(db: Database, brain) -> dict:
    """Process unconsolidated corrections into the preference store.

    Returns a summary dict with keys: analyzed, applied, superseded,
    forgotten, nudge, quote, sections (for export) — or None when idle.
    """
    episodes = await db.get_unconsolidated_corrections()
    episodes = episodes[:RUN_CAP]
    if not episodes:
        return {}

    episodes.sort(key=lambda c: c["created_at"])
    input_text = _render_episodes(episodes)
    input_text = input_text[:MAX_INPUT_CHARS]

    analysis = await _analyze(db, brain, input_text)
    statements = analysis.pop("statements", []) or []
    nudge = (analysis.get("nudge") or "").strip() or None

    summary = {"analyzed": len(episodes), "applied": [], "superseded": [], "forgotten": []}

    # Apply each statement through the (deterministic) merge pipeline.
    log_lines: list[str] = []
    for st in statements:
        if not isinstance(st, dict):
            continue
        section = str(st.get("section") or "preferences")
        if section not in ACTING_SECTIONS:
            continue
        sentence = (st.get("sentence") or "").strip()[:MAX_STATEMENT_CHARS]
        if not sentence:
            continue
        outcome, pid = await db.merge_fact(
            fact=sentence,
            category=str(st.get("category") or "personal"),
            keywords=st.get("keywords") or "",
            confidence=max(0.35, min(0.9, float(st.get("confidence") or 0.6))),
            evidence_ref="consolidation:nightly",
        )
        if outcome == "superseded":
            summary["superseded"].append(sentence)
        else:
            summary["applied"].append(sentence)
        log_lines.append(f"- {sentence}")

    # `forgotten` — only acted on explicitly (cold, low-evidence patterns).
    for st in statements:
        if not isinstance(st, dict):
            continue
        if st.get("forget"):
            sentence = (st.get("sentence") or "").strip()[:MAX_STATEMENT_CHARS]
            if not sentence:
                continue
            await db.apply_decay()
            summary["forgotten"].append(sentence)

    # §6.2 cap: never exceed MAX_PREFERENCES live rows.
    dropped = await db.enforce_pref_cap()
    summary["dropped"] = dropped

    await db.mark_corrections_consolidated([e["id"] for e in episodes])

    # Compose the owner-facing digest proposal.
    sections = [{"title": "Nightly consolidation", "body": "\n".join(log_lines) or "_none_"}] if log_lines else []
    if nudge:
        sections.append({"title": "Note", "body": nudge[:MAX_QUOTE_CHARS]})
    sections = sections[: MAX_CONSOLIDATION_SECTIONS // 2 + 1]

    summary.update(
        {
            "nudge": nudge,
            "sections": sections,
            "quote": (analysis.get("quote") or "").strip()[:MAX_QUOTE_CHARS],
        }
    )

    await _deliver_digest(stats=summary, sections=sections, nudge=nudge)
    return summary


async def _analyze(db: Database, brain, input_text: str) -> dict:
    """Ask the deep tier to distill episodes into bounded statements."""
    from agent.prompts import LOOP_ANALYST_SYSTEM

    prompt = (
        "Today's UTC date: "
        + datetime.now(timezone.utc).strftime("%Y-%m-%d")
        + "\nCORRECTIONS TO CONSOLIDATE:\n"
        + input_text
        + "\n\nReturn strictly valid JSON only (no prose, no fences), conforming to:\n"
        + JSON_SCHEMA
    )
    text = await brain._generate(
        tier="deep",
        messages=[{"role": "user", "content": prompt}],
        system_instruction=LOOP_ANALYST_SYSTEM,
        temperature=0.3,
        max_tokens=1400,
    )
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        obj = _fallback_parse(text)

    if not isinstance(obj, dict):
        logger.warning("Consolidation model returned unusable output; using fallback.")
        return _fallback_result(input_text)
    return {
        "statements": obj.get("statements") or [],
        "nudge": obj.get("nudge") or None,
    }


def _render_episodes(episodes: list[dict]) -> str:
    lines: list[str] = []
    for e in episodes:
        when = (e.get("created_at") or "")[:16]
        user = (e.get("user_message") or "").strip()
        detail = (e.get("correction") or "").strip()
        why = (e.get("agent_action") or "").strip()
        if user == detail:
            user = f"user: {user}"
        else:
            user = f"user: {user}" if user else ""
        lines.append(
            f"[{when}] {user}"
            f"{(', detail: ' + detail) if detail else ''}"
            f"{(', after: ' + why[:120]) if why else ''}"
        )
    return "\n".join(lines)


JSON_SCHEMA = """{
  "statements": [
    {
      "sentence": "a single durable preference/fact",
      "section": "preferences|principles",
      "category": "personal|diet|work|health|home|social|finance|travel",
      "keywords": "few space separated FTS tokens",
      "confidence": "0.0 to 1.0",
      "forget": false
    }
  ],
  "nudge": "one short paragraph or null"
}
"""


def _fallback_parse(text: str) -> dict:
    """Parse a brace-delimited JSON object out of noisy model output."""
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except (json.JSONDecodeError, TypeError):
            pass
    return {}


def _fallback_result(input_text: str) -> dict:
    """Guaranteed-safe fallback: never fabricate preferences from noise."""
    return {"statements": [], "nudge": "Consolidation produced no reliable statements this run."}


async def _deliver_digest(*, stats: dict, sections: list[dict], nudge: str | None) -> None:
    """Send the consolidated digest to the owner as a single proposal."""
    if not sections:
        logger.info("Consolidation: nothing to report this run.")
        return
    quote = stats.get("quote")
    old = ""
    await send_proposal(
        old,
        "\n".join(s.get("body", "") for s in sections),
        nudge or "Nightly consolidation digest.",
        kind="consolidation",
        meta={"kind": "consolidation", "summary": stats},
    )


# ─── Weekly review (Phase 7 §7.3) ─────────────────────────────────────────


async def weekly_conversation_review(db: Database, brain) -> str | None:
    """Phase 7 §7.3 weekly review (Friday 17:00): completed vs slipped, and
    one observed pattern about how the week actually went.

    Single deep-tier call over deterministic inputs (tasks completed in the
    last 7 days, tasks started-but-still-slipped, and a sample of the week's
    user messages). Returns the review text or None when idle.
    """
    now = datetime.now(Config.TIMEZONE).replace(tzinfo=None)
    since = (now - timedelta(days=7)).isoformat()

    done = await db.get_tasks_completed_since(since)
    finished = [t["description"] for t in done[-8:]]

    today = now.strftime("%Y-%m-%d")
    week_start = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    open_tasks = await db.get_overdue_active_tasks(today)
    slipped = [
        t.description
        for t in open_tasks
        if t.due_date and week_start <= t.due_date < today
    ][:5]

    convos = await db.get_conversations_since(since)
    user_msgs = [c.content for c in convos if c.role == "user"][-40:]

    if not (finished or slipped or user_msgs):
        return None

    prompt = (
        "Short, warm, personal-assistant tone. One 'Pattern:' observation max — "
        "a real, honest observation about how the week went. Do NOT invent "
        "anything not in the input.\n\n"
        f"TASKS COMPLETED:\n{finished}\n\n"
        f"TASKS STARTED BUT SLIPPED:\n{slipped}\n\n"
        f"WEEK OF CONVERSATION:\n{user_msgs}\n\n"
        "Return at most 6 short sentences."
    )
    text = await brain._generate(
        tier="deep",
        messages=[{"role": "user", "content": prompt}],
        system_instruction=(
            "Write a warm, specific weekly review. Structure it around: "
            "'Done:' (completed), 'Slipped:' (not finished), and 'Pattern:' "
            "(one observation). Max two sentences each."
        ),
        temperature=0.7,
        max_tokens=1200,
    )
    if not text or len(text.strip()) < 10:
        return None
    return text.strip()


__all__ = [
    "consolidate_nightly",
    "weekly_conversation_review",
    "consolidate_with_blame",
]


async def consolidate_with_blame(db: Database, statements: list) -> list[dict]:
    """Apply statements and, for contradictions, produce blame UI entries.

    Runs the same merge pipeline but returns per-statement outcomes so the
    caller (bot / nightly) can render an Approve/Reject proposal with 'this
    replaces your previous preference X' lines.
    """
    results: list[dict] = []
    for st in statements:
        st = st or {}
        sentence = (st.get("sentence") or "").strip()[:MAX_STATEMENT_CHARS]
        if not sentence:
            continue
        outcome, pid = await db.merge_fact(
            fact=sentence,
            category=str(st.get("category") or "personal"),
            keywords=st.get("keywords") or "",
            confidence=max(0.35, min(0.9, float(st.get("confidence") or 0.6))),
            evidence_ref="consolidation:with_blame",
        )
        results.append({"sentence": sentence, "outcome": outcome, "id": pid})
    if results:
        await db.enforce_pref_cap()
    return results