"""Unit tests for the Phase 6 learning loop.

Covers the closed loop: merge_fact (match / supersede / new), retrieval over
injection (§6.4), nightly consolidation (§6.3), the persona proposal
pipeline (§6.5) and the messaging bridge (§6.10).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.brain import SecondBrain
from services.messaging import FakeProposalSender, set_proposal_sender
from storage.database import Database, contains_negation, normalize_fact
from storage.models import PreferenceCreate


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "test.db"))
    await db.connect()
    yield db
    await db.close()


@pytest.fixture
def fake_sender() -> FakeProposalSender:
    sender = FakeProposalSender()
    set_proposal_sender(sender)
    return sender


@pytest.fixture
def clear_sender():
    yield
    set_proposal_sender(None)


# ─── normalize_fact ────────────────────────────────────────────────────────


def test_normalize_fact() -> None:
    # pronoun/softener tokens are dropped deterministically
    assert normalize_fact("The user prefers coffee") == normalize_fact("prefers coffee")
    # negation markers and helper verbs drop to the same base form, so a
    # negated restatement can supersede its positive counterpart (§6.2)
    assert normalize_fact("I don't like black coffee") == normalize_fact("I like black coffee")
    assert normalize_fact("I do not drink tea") == normalize_fact("I drink tea")
    assert contains_negation("I don't like black coffee")
    assert not contains_negation("I like black coffee")
    # word-boundary safety: "now" is not negation, "piano" is untouched
    assert contains_negation("i no longer drink tea")
    assert not contains_negation("lets buy a piano now")


# ─── merge_fact ────────────────────────────────────────────────────────────


async def test_merge_new_and_match(db: Database) -> None:
    outcome, pid = await db.merge_fact(
        "Prefers to schedule workouts in the morning",
        category="health",
        keywords="workout morning",
        confidence=0.7,
    )
    assert outcome == "new"
    assert pid > 0

    # Exact restatement → evidence bump, no duplicate.
    outcome2, pid2 = await db.merge_fact(
        "Prefers to schedule workouts in the morning",
        confidence=0.7,
    )
    assert outcome2 == "matched"
    assert pid2 == pid

    prefs = await db.get_all_preferences()
    assert len(prefs) == 1
    assert prefs[0].evidence_count == 2
    assert prefs[0].confidence > 0.7


async def test_merge_supersedes_never_overwrites(db: Database) -> None:
    await db.merge_fact("Prefers to run at night", confidence=0.7)
    outcome, new_id = await db.merge_fact("Does not run at night", confidence=0.7)

    assert outcome == "superseded"

    prefs = await db.get_all_preferences()
    assert len(prefs) == 2
    old = next(p for p in prefs if p.superseded_by is not None)
    new = next(p for p in prefs if p.id == new_id)
    assert old.superseded_by == new.id
    assert new.superseded_by is None
    # history is retained, never overwritten
    assert "Prefers to run at night" in old.fact


async def test_merge_fts_similar_match(db: Database) -> None:
    await db.merge_fact(
        "Prefers strong black coffee at breakfast",
        keywords="coffee breakfast",
        confidence=0.7,
    )
    outcome, pid = await db.merge_fact(
        "Likes strong black coffee at breakfast",
        keywords="coffee breakfast",
        confidence=0.7,
    )
    # bases differ only by prefer→like → FTS token-subset match, not a new row
    assert outcome in ("matched", "superseded")
    assert len(await db.get_all_preferences()) == 1
    row = (await db.get_all_preferences())[0]
    assert row.id == pid
    assert row.evidence_count == 2


# ─── retrieval-over-injection (§6.4) ───────────────────────────────────────


async def test_context_preferences_cores_always_present(db: Database) -> None:
    await db.merge_fact("Core fact about running", confidence=0.9, evidence_ref="x")
    await db.db.execute(
        "UPDATE preferences SET is_core = 1 WHERE confidence >= 0.8"
    )
    await db.db.commit()
    await db.merge_fact("Vegan diet", confidence=0.4)

    ctx = await db.get_context_preferences("totally unrelated query here random words")
    facts = [p.fact for p in ctx]
    assert any("Core fact" in f for f in facts)  # core always injected
    assert not any("Vegan" in f for f in facts)  # low confidence, unrelated


async def test_context_preferences_topic_match(db: Database) -> None:
    await db.merge_fact("Prefers espresso over drip coffee", confidence=0.5)
    ctx = await db.get_context_preferences("what coffee should i buy?")
    assert any("espresso" in p.fact for p in ctx)


async def test_superseded_rows_never_injected(db: Database) -> None:
    await db.merge_fact("Prefers to wake up at 5am", confidence=0.6)
    await db.merge_fact("Does not wake up at 5am anymore", confidence=0.6)

    ctx = await db.get_context_preferences("wake up at 5am habit")
    assert not any("Prefers to wake up" in p.fact for p in ctx)  # superseded → gone
    assert any("Does not wake up" in p.fact for p in ctx)        # the live fact
    assert ctx and (await db.get_all_preferences())[0].superseded_by is not None


# ─── nightly consolidation (§6.3) ──────────────────────────────────────────


class _StubBrain(SecondBrain):
    """Brain stub: _generate returns canned consolidation JSON."""

    canned: str = (
        '{"statements": ['
        '{"sentence": "Prefers meetings before noon", "section": "preferences", '
        '"confidence": 0.8},'
        '{"sentence": "Does not like last-minute changes", "section": "preferences", '
        '"confidence": 0.6}],'
        '"nudge": "You seem to prefer mornings for focused work."}'
    )

    async def _generate(self, *args, **kwargs) -> str:
        return self.canned


async def test_nightly_consolidation_applies_and_flags(db: Database, fake_sender) -> None:
    await db.add_correction(
        trigger="explicit",
        user_message="i prefer meetings before noon",
        agent_action="scheduled a 4pm meeting",
        correction="schedule my meetings in the morning",
    )
    await db.add_correction(
        trigger="explicit",
        user_message="please don't reorder my tasks without asking",
        agent_action="reordered tasks",
        correction="ask first before changing my task order",
    )

    brain = _StubBrain(db)
    summary = await __import__(
        "services.consolidation", fromlist=["consolidate_nightly"]
    ).consolidate_nightly(db, brain)

    assert summary["analyzed"] == 2
    assert any("noon" in s for s in summary["applied"])
    assert summary["nudge"]

    # corrections get flagged as consolidated
    remaining = await db.get_unconsolidated_corrections()
    assert remaining == []

    # the digest reached the (fake) owner
    assert len(fake_sender.sent) == 1


async def test_nightly_consolidation_idles_when_no_corrections(db: Database, fake_sender) -> None:
    summary = await __import__(
        "services.consolidation", fromlist=["consolidate_nightly"]
    ).consolidate_nightly(db, _StubBrain(db))
    assert summary == {}
    assert fake_sender.sent == []


# ─── persona proposal (§6.5) ───────────────────────────────────────────────


async def test_persona_proposal_draft_and_apply(db: Database, fake_sender) -> None:
    from services.persona import apply_persona_proposal, build_persona_proposal

    await db.add_correction(trigger="explicit", user_message="keep replies short")

    brain = _StubBrain(db)
    brain.canned = (
        '{"principles": ["Keep replies under 200 words", '
        '"Confirm before mutating user data"], '
        '"rationale": "The user prefers brevity and control.", '
        '"risk": "Do not become curt or invasive."}'
    )

    proposed = await build_persona_proposal(db, brain, live=False)
    assert proposed
    assert "under 200 words" in proposed

    # stored as an INACTIVE proposal version until approved
    versions = await db.get_all_persona_versions()
    assert len(versions) == 1
    assert versions[0]["applied"] == 0
    assert await db.get_active_persona() is None  # not steering yet

    version_id = versions[0]["id"]
    reply = await apply_persona_proposal(db, version_id, "approve")
    assert "approved" in reply.lower()
    active = await db.get_active_persona()
    assert active and "under 200 words" in active


async def test_persona_proposal_reject_keeps_old(
    db: Database, fake_sender, clear_sender
) -> None:
    from services.persona import apply_persona_proposal, build_persona_proposal

    old_id = await db.save_persona_version("OLD principles block", "operating_principles")
    await db.set_persona_applied(old_id, 1)

    brain = _StubBrain(db)
    brain.canned = (
        '{"principles": ["New principle A"], '
        '"rationale": "prefers it", "risk": "none"}'
    )
    await build_persona_proposal(db, brain, live=False)

    versions = await db.get_all_persona_versions()
    new_version = versions[-1]
    assert new_version["applied"] == 0
    reply = await apply_persona_proposal(db, new_version["id"], "reject")
    assert "kept" in reply.lower()
    assert (await db.get_active_persona()) == "OLD principles block"


async def test_persona_proposal_sends_via_bridge(
    db: Database, fake_sender, clear_sender
) -> None:
    from services.persona import build_persona_proposal

    brain = _StubBrain(db)
    brain.canned = (
        '{"principles": ["Rule one", "Rule two"], "rationale": "r", "risk": "z"}'
    )
    await build_persona_proposal(db, brain, live=True)
    assert len(fake_sender.sent) == 1
    meta = fake_sender.sent[0][3]
    assert meta.get("kind") == "operating_principles"
    assert meta.get("version_id") is not None


# ─── legacy save_preference still works ────────────────────────────────────


async def test_save_preference_uses_learning_loop(db: Database) -> None:
    await db.save_preference(PreferenceCreate(key="drink", value="prefers green tea"))
    prefs = await db.get_all_preferences()
    assert len(prefs) == 1
    assert prefs[0].key == "drink"
    assert "green tea" in prefs[0].fact