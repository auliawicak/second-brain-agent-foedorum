"""Unit tests for the confirmation gate (Phase 2 'ask before acting')."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.confirmation import (
    CONFIRMING_TOOLS,
    confirmation_question,
    is_confirmation,
)
from agent.brain import ChatResult, SecondBrain
from storage.database import Database

# ─── helper behaviour ───────────────────────────────────────────────────────


def test_is_confirmation() -> None:
    for yes in (
        "yes",
        "yep go ahead",
        "sure, add it",
        "okay sounds good",
        "Please do it",
        "go ahead and complete task 3",
        "confirm",
        "Yeah!",
        "Y",
    ):
        assert is_confirmation(yes), yes
    for no in (
        "add buy milk",
        "what time is it",
        "maybe later",
        "no, don't",
        "why did you do that",
        "i don't want that",
    ):
        assert not is_confirmation(no), no


def test_confirmation_questions() -> None:
    q = confirmation_question("add_task", {"description": "buy milk", "due_date": "2026-09-07"})
    assert q == "Do you want me to add the task **buy milk** (due 2026-09-07)?"
    q = confirmation_question("complete_task", {"task_id": 3})
    assert "#3" in q
    q = confirmation_question("save_note", {"content": "idea: garden"})
    assert "garden" in q
    q = confirmation_question("set_reminder", {"message": "call mom", "trigger_time": "2026-09-07T10:00:00"})
    assert "10:00" in q
    q = confirmation_question("save_preference", {"key": "drink", "value": "black coffee"})
    assert "black coffee" in q
    assert confirmation_question("list_tasks", {}) is None
    assert confirmation_question("add_task", {}) is None


def test_confirming_tools_are_the_mutators() -> None:
    assert CONFIRMING_TOOLS == {
        "add_task",
        "complete_task",
        "save_note",
        "set_reminder",
        "save_preference",
        "remember_fact",
        "record_correction",
    }


# ─── gate behaviour in chat() ───────────────────────────────────────────────


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "test.db"))
    await db.connect()
    yield db
    await db.close()


@pytest.fixture
async def brain(db: Database, monkeypatch) -> SecondBrain:
    brain = SecondBrain(db)
    await brain.start()

    async def fake_generate(self, *args, **kwargs):
        return '{"tool": "add_task", "args": {"description": "buy milk", "priority": "low"}}'

    # stateful: first call proposes the tool, later calls summarise.
    gen_state = {"calls": 0}

    async def stateful_generate(self, *args, **kwargs):
        gen_state["calls"] += 1
        if gen_state["calls"] == 1:
            return '{"tool": "add_task", "args": {"description": "buy milk", "priority": "low"}}'
        return "Done — added the task."

    monkeypatch.setattr(SecondBrain, "_generate", stateful_generate)
    yield brain
    await brain.stop()


async def test_free_text_mutation_is_confirmed_first(brain: SecondBrain, db: Database) -> None:
    result = await brain.chat("add buy milk to my tasks")
    assert isinstance(result, ChatResult)
    assert result.executed_tool is False
    assert "buy milk" in result.text and "?" in result.text
    assert await db.get_tasks() == []          # nothing executed yet
    assert brain._pending_confirmation is not None

    # Saying yes resumes the exact pending action and completes it.
    result2 = await brain.chat("yes")
    assert result2.text == "Done — added the task."
    tasks = await db.get_tasks()
    assert len(tasks) == 1
    assert tasks[0].description == "buy milk"  # not duplicated on confirm
    assert brain._pending_confirmation is None


async def test_slash_command_skips_confirmation(brain: SecondBrain, db: Database) -> None:
    result = await brain.chat(
        "Create a new task for me: buy milk. Use the add_task tool.",
        confirmed=True,
    )
    assert result.text == "Done — added the task."
    assert result.executed_tool is True
    assert len(await db.get_tasks()) == 1


async def test_read_tools_run_without_confirmation(db: Database, monkeypatch) -> None:
    from agent.brain import SecondBrain
    from storage.models import TaskCreate

    b = SecondBrain(db)
    await b.start()
    await db.add_task(TaskCreate(description="existing one"))

    async def read_generate(self, *args, **kwargs):
        return '{"tool": "list_tasks", "args": {"status": "all"}}'

    monkeypatch.setattr(SecondBrain, "_generate", read_generate)
    result = await b.chat("what tasks exist")
    assert result.executed_tool is True        # read executed immediately
    assert "existing one" in (result.tool_result or "")
    assert b._pending_confirmation is None
    await b.stop()