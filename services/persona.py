"""Monthly operating-principles proposal (Phase 6 §6.5).

Every ~4 weeks the agent reads what it has learned (corrections,
consolidated preferences, completed work) and drafts a *candidate*
"operating principles" block. It is sent to the owner as an interactive
proposal with Approve/Reject buttons, and only lands in the active persona
(→ every future system prompt) when the owner approves.

Decisions are persisted in `persona_versions`; Apply records a new applied
version, Reject writes the proposal as an inactive (rejected) version so we
can show drift over time. The proposal is ALSO written to the vault by the
markdown export job, regardless of outcome, so the full history stays local.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from services.messaging import send_proposal

logger = logging.getLogger(__name__)

PERSONA_KIND = "operating_principles"
PROPOSAL_TTL_SECONDS = 7 * 24 * 3600  # a week to decide, then it expires

SYSTEM_LINE = (
    "You are a personal AI 'second brain'. You speak naturally, short, warm, "
    "no bullet walls. Draft operating principles from the evidence below. "
    "Principles are short, imperative, actionable rules — not raw preferences. "
    "Follow this exact JSON-only schema:\n"
    '{"principles": ["<rule>", ...], "rationale": "<one paragraph why>", '
    '"risk": "<what this must not do / edge cases>"}'
)


async def _current_principles(db) -> str | None:
    """Current principles from the data persona (Phase 8), falling back to
    the §6.5 vote table so pre-Phase-8 rows still show."""
    config = await db.get_active_persona_config()
    if config and (config.get("principles") or "").strip():
        return config["principles"].strip()
    return await db.get_active_persona(PERSONA_KIND)


async def gather_persona_proposal_brief(db, window_days: int = 28) -> str:
    """Assemble the evidence brief that steers the persona candidate."""
    since = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    lines = [f"Generate the next operating-principles update. Window: last {window_days} days."]

    corrections = await db.get_corrections_since(since)
    if corrections:
        lines.append("CORRECTIONS (do not repeat these mistakes):")
        for c in corrections[-8:]:
            user = c.get("user_message") or ""
            detail = c.get("correction") or ""
            lines.append(f"- {user or detail} [trigger={c.get('trigger')}]")
    else:
        lines.append("CORRECTIONS: none in window.")

    prefs = await db.get_all_preferences()
    live = [p for p in prefs if p.superseded_by is None and p.confidence >= 0.3]
    if live:
        lines.append("STRONG PREFERENCES (confidence >= threshold):")
        for p in sorted(live, key=lambda x: (-x.confidence, -x.evidence_count))[:10]:
            lines.append(
                f"- {p.fact} (confidence={p.confidence:.2f}, evidence={p.evidence_count})"
            )
    else:
        lines.append("PREFERENCES: none yet.")

    done = await db.get_tasks_completed_since(since)
    if done:
        lines.append("RECENTLY COMPLETED WORK:")
        for t in done[-6:]:
            lines.append(f"- {t['description']}")
    else:
        lines.append("COMPLETED WORK: none in window.")

    extra = await _current_principles(db)
    if extra:
        lines.append("\nCURRENT OPERATING PRINCIPLES (only change what the evidence supports):")
        lines.append(extra[:2000])
    return "\n".join(lines)


async def build_persona_proposal(db, brain, *, live: bool = True) -> str | None:
    """Draft a candidate operating-principles block and (when live) send it.

    Returns the new principles text when drafted, else None. Approval is
    applied later via `apply_proposal_result`.
    """
    brief = await gather_persona_proposal_brief(db)

    date_line = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prompt = (
        f"Today's UTC date: {date_line}\n"
        f"Strictly JSON only. Output rules:\n"
        f'- "principles": 3 to 6 short imperative rules of one sentence each. '
        f"No mention of raw evidence, percentages, or 'I learned'.\n"
        f"- Incorporates only what the evidence supports — do not invent habits.\n"
        f"- 'rationale': one short paragraph connecting principles to evidence.\n"
        f"- 'risk': one sentence on what this must not do.\n\n{brief}"
    )

    text = await brain._generate(
        tier="deep",
        messages=[{"role": "user", "content": prompt}],
        system_instruction=SYSTEM_LINE,
        temperature=0.6,
        max_tokens=2000,
    )

    parsed = _digest_principles(text)
    if not parsed:
        logger.warning("Persona generator produced no usable principles.")
        return None

    old = await _format_principles_for_ui(db)

    new_principles = parsed.get("principles") or []
    new_principles_block = "\n".join(f"- {p}" for p in new_principles)
    rationale = parsed.get("rationale") or ""
    risk = parsed.get("risk") or ""

    ui_cmds = (
        f"**Persona updates?**\n\n"
        f"> Evidence: {brief[:400]}\n\n"
        f"**Proposed principles**\n{new_principles_block or '_none_'}\n\n"
        f"*Rationale:* {rationale[:200]}\n"
        f"*Risk:* {risk[:200]}\n\n"
        f"Approve to activate now and from every future prompt."
    )

    await db.save_persona_version(
        new_principles_block + "\n\n" + rationale,  # stored even if unapproved
    )
    version_row = await db.get_all_persona_versions(PERSONA_KIND)
    version_id = version_row[-1]["id"] if version_row else None

    if live:
        token = await send_proposal(
            old,
            ui_cmds,
            f"{rationale}\n\nApprove/Reject decides whether these become active. "
            f"If approved, they steer every future reply.",
            meta={"kind": PERSONA_KIND, "version_id": version_id},
        )
        logger.info("Persona proposal sent (token=%s).", token)
    else:
        logger.info("Persona proposal drafted (headless, not sent).")
    return new_principles_block


def _digest_principles(text: str) -> dict | None:
    """Parse principles + rationale from the generator output."""
    from agent.parsing import extract_json_object

    obj = extract_json_object(text)
    if not isinstance(obj, dict):
        return None
    principles = obj.get("principles") or []
    if isinstance(principles, str):
        principles = [principles]
    principles = [str(p).strip() for p in principles if str(p).strip()]
    if not principles:
        return None
    return {
        "principles": principles[:6],
        "rationale": (obj.get("rationale") or "").strip(),
        "risk": (obj.get("risk") or "").strip(),
    }


async def _format_principles_for_ui(db) -> str:
    """Render the currently-active principles for the Approve dialog."""
    active = await _current_principles(db)
    if not active:
        return "_none yet_"
    return active[:400]


async def apply_persona_proposal(db, version_id: int | None, outcome: str) -> str:
    """Apply an Approve/Reject decision for a persona proposal.

    Approval flips the stored version to `applied=1` (new active persona)
    AND syncs the approved principles into the Phase 8 `persona` table,
    carrying voice and mode rules forward — so `/persona show` and the
    per-turn prompt both agree. Rejection keeps the version stored but
    inactive. Returns a confirmation.
    """
    if version_id is None:
        return "⚠️ No pending persona proposal to act on."
    if outcome == "approve":
        await db.set_persona_applied(version_id, applied=1)
        await _sync_approved_principles(db, version_id)
        return "✅ Operating principles approved and activated."
    await db.set_persona_applied(version_id, applied=0)
    return "❌ Persona update rejected — existing principles kept."


async def _sync_approved_principles(db, version_id: int) -> None:
    """Land an approved proposal into the data `persona` table.

    The stored version content is "<principles block>\n\n<rationale>";
    only the principles block is promoted, voice/mode rules carry forward,
    and a new active snapshot is created (Phase 8 §8.1).
    """
    row = await db.get_persona_version(version_id)
    if not row:
        return
    content = (row.get("content") or "").strip()
    principles = content.split("\n\n", 1)[0].strip()
    if not principles:
        return
    snapshot = await db.save_persona_snapshot(principles=principles)
    await db.set_persona_active(snapshot["version"])
    logger.info("Approved persona synced into data persona v%d", snapshot["version"])