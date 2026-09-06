"""Unit tests for Phase 8 — persona as data (§8).

Covers:
- §8.1 migration seeds persona v1 (voice/principles/mode_rules, one active row)
- §8.2 every edit is a new active snapshot carrying the other layers forward,
  with only one row ever active
- live edit takes effect on the "next turn" (DB re-read per turn, no restart)
- rollback is an exact flag flip back to a prior snapshot
- persona assembly order Voice → Principles → Mode rules, capped by the
  Phase 3 prompt-size assertion
- §6.5↔§8.2: approving a persona proposal lands principles in the persona table
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.context import assemble_persona_block, build_system_prompt
from agent.prompts import MAIN_PERSONA
from config import Config
from services.persona import apply_persona_proposal
from storage.database import Database


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "test.db"))
    await db.connect()
    yield db
    await db.close()


# ─── §8.1 migration seed ────────────────────────────────────────────────────


async def test_migration_seeds_persona_v1(db: Database) -> None:
    persona = await db.get_active_persona_config()
    assert persona is not None
    assert persona["version"] == 1
    assert "Bahasa" in persona["voice"]
    principles = persona["principles"].splitlines()
    assert len(principles) == 6
    assert all(p.startswith("- ") for p in principles)
    assert persona["mode_rules"]
    assert "06:00" in persona["mode_rules"]

    versions = await db.list_persona_versions()
    assert len(versions) == 1
    assert versions[0]["active"] == 1


async def test_only_one_active_persona_row(db: Database) -> None:
    versions = await db.list_persona_versions(limit=100)
    active = [v for v in versions if v["active"] == 1]
    assert len(active) == 1


# ─── §8.2 persona_set — live edit, no restart ───────────────────────────────


async def test_set_voice_changes_active_config_and_carries_principles(db: Database) -> None:
    from services.persona_control import persona_set

    reply = await persona_set(db, "voice", "Formal British English, no emoji.")
    assert "v2" in reply

    persona = await db.get_active_persona_config()
    assert persona["version"] == 2
    assert persona["voice"] == "Formal British English, no emoji."
    # the untouched layer (principles) is carried forward unchanged
    assert persona["principles"].splitlines()[0].startswith("- Default to doing")

    versions = await db.list_persona_versions()
    assert len(versions) == 2
    # newest-first (v2 active, v1 inactive)
    assert [v["active"] for v in versions] == [1, 0]

    # "next turn": a fresh read (what brain.chat does per turn) sees it
    assert await db.get_active_persona_config() == persona


async def test_set_principles_carries_voice_forward(db: Database) -> None:
    from services.persona_control import persona_set

    seed_voice = (await db.get_active_persona_config())["voice"]
    await persona_set(db, "principles", "- Never disagree.")

    persona = await db.get_active_persona_config()
    assert persona["voice"] == seed_voice
    assert persona["principles"] == "- Never disagree."
    assert persona["version"] == 2


async def test_persona_set_invalid_layer_rejected(db: Database) -> None:
    from services.persona_control import persona_set

    reply = await persona_set(db, "wat", "oops")
    assert "Unknown layer" in reply
    assert (await db.get_active_persona_config())["version"] == 1


async def test_persona_set_requires_text(db: Database) -> None:
    from services.persona_control import persona_set

    reply = await persona_set(db, "voice", "   ")
    assert "Missing text" in reply
    assert (await db.get_active_persona_config())["version"] == 1


# ─── §8.2 rollback — exact flag flip ────────────────────────────────────────


async def test_rollback_restores_exact_snapshot(db: Database) -> None:
    from services.persona_control import persona_rollback, persona_set

    seed = await db.get_active_persona_config()
    await persona_set(db, "voice", "Voice A")
    await persona_set(db, "voice", "Voice B")
    assert (await db.get_active_persona_config())["voice"] == "Voice B"

    reply = await persona_rollback(db, version=2)
    assert "v2" in reply
    persona = await db.get_active_persona_config()
    assert persona["version"] == 2
    # exactly the previous snapshot, layers unchanged
    assert persona["voice"] == "Voice A"
    assert persona["principles"] == seed["principles"]

    # rollback to the seed restores v1 too
    await persona_rollback(db, version=1)
    persona = await db.get_active_persona_config()
    assert persona["version"] == 1
    assert persona["voice"] == seed["voice"]
    assert persona["principles"] == seed["principles"]


async def test_rollback_missing_version_rejected(db: Database) -> None:
    from services.persona_control import persona_rollback

    reply = await persona_rollback(db, version=999)
    assert "No persona v999" in reply
    assert (await db.get_active_persona_config())["version"] == 1


async def test_rollback_to_already_active_is_noop(db: Database) -> None:
    from services.persona_control import persona_rollback

    reply = await persona_rollback(db, version=1)
    assert "already active" in reply
    assert (await db.get_active_persona_config())["version"] == 1


# ─── §8.2 history ───────────────────────────────────────────────────────────


async def test_history_lists_versions_newest_first(db: Database) -> None:
    from services.persona_control import persona_history, persona_set

    await persona_set(db, "voice", "Legal and precise.")
    await persona_set(db, "principles", "- Verify before answering.")

    text = await persona_history(db)
    assert "v1" in text and "v2" in text and "v3" in text
    assert text.index("v3") < text.index("v2") < text.index("v1")


# ─── §8.1 assembly order + Phase 3 cap ──────────────────────────────────────


def test_assemble_persona_block_order() -> None:
    block = assemble_persona_block(
        voice="Short, warm.",
        principles="- Push back.",
        mode_rules="Work: terse.",
    )
    order = [
        block.index("## Voice"),
        block.index("## Principles"),
        block.index("## Mode Rules"),
    ]
    assert order == sorted(order)


def test_assemble_persona_block_skips_blank_layers() -> None:
    assert assemble_persona_block(voice="x", principles="", mode_rules=None) == "## Voice\nx"


async def test_data_persona_stays_within_prompt_cap(db: Database) -> None:
    """Phase 3 prompt-size assertion applies to the assembled data persona:
    even a pathological persona block can never blow MAX_PROMPT_CHARS."""
    huge = ("- rule one two three. " * 2000).strip()
    from services.persona_control import persona_set

    reply = await persona_set(db, "principles", huge)
    assert "v2" in reply

    persona = await db.get_active_persona_config()
    block = assemble_persona_block(
        voice=persona["voice"],
        principles=persona["principles"],
        mode_rules=persona["mode_rules"],
    )
    prompt = build_system_prompt(MAIN_PERSONA, sections=[block])
    assert len(prompt) <= Config.MAX_PROMPT_CHARS
    # the core persona is protected even when the persona block is enormous
    assert MAIN_PERSONA in prompt


# ─── §6.5 ↔ §8.2 approval syncs into the persona table ──────────────────────


async def test_approve_proposal_syncs_principles_into_data_persona(db: Database) -> None:
    seed = await db.get_active_persona_config()

    proposed_block = "- Always verify.\n- Say when you are unsure."
    rationale = "Because the user kept catching mistakes."
    await db.save_persona_version(proposed_block + "\n\n" + rationale)
    version_id = (await db.get_all_persona_versions()) [-1]["id"]

    reply = await apply_persona_proposal(db, version_id=version_id, outcome="approve")
    assert "approved" in reply

    persona = await db.get_active_persona_config()
    assert persona["version"] == 2
    assert persona["principles"] == proposed_block
    # voice + mode rules carried forward, untouched
    assert persona["voice"] == seed["voice"]
    assert persona["mode_rules"] == seed["mode_rules"]


async def test_reject_proposal_keeps_persona_table_untouched(db: Database) -> None:
    await db.save_persona_version("- Wild rule.\n\nbecause")
    version_id = (await db.get_all_persona_versions()) [-1]["id"]

    reply = await apply_persona_proposal(db, version_id=version_id, outcome="reject")
    assert "rejected" in reply
    assert (await db.get_active_persona_config())["version"] == 1