"""Unit tests for definitive reminder lookup + absence-answering behavior.

Covers:
- `get_reminders` tool: empty DB -> explicit "no reminders", populated DB ->
  lists them (incl. recurring + inactive), optional substring filter
- `get_all_reminders` DB method
- MAIN_PERSONA instructs the model to answer YES/NO from the DB and never
  reply "I'll check" without giving the result
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.prompts import MAIN_PERSONA
from agent.tools import get_reminders, set_database
from storage.database import Database
from storage.models import ReminderCreate


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "test.db"))
    await db.connect()
    set_database(db)
    yield db
    set_database(None)
    await db.close()


async def test_get_reminders_empty_is_explicit(db: Database) -> None:
    result = await get_reminders()
    assert "No reminders are set up." in result


async def test_get_reminders_empty_with_query(db: Database) -> None:
    result = await get_reminders(query="prayer")
    assert "No reminders match 'prayer'." in result


async def test_get_reminders_lists_active_one(db: Database) -> None:
    await db.add_reminder(
        ReminderCreate(
            message="Fajr (Subuh) Prayer",
            trigger_time="2026-09-07T05:00:00",
            is_recurring=True,
            cron_expression="0 5 * * *",
        )
    )
    result = await get_reminders()
    assert "Active reminders (1)" in result
    assert "Fajr (Subuh) Prayer" in result
    assert "recurring: 0 5 * * *" in result


async def test_get_reminders_ignores_inactive_by_default(db: Database) -> None:
    await db.add_reminder(
        ReminderCreate(message="One-off", trigger_time="2026-09-05T11:54:30")
    )
    cursor = await db.db.execute(
        "UPDATE reminders SET is_active = 0 WHERE message = ?", ("One-off",)
    )
    await db.db.commit()
    await cursor.close()

    assert "No reminders are set up." in await get_reminders()
    assert "One-off" in await get_reminders(active_only=False)
    assert "[inactive]" in await get_reminders(active_only=False)


async def test_get_reminders_query_filters_by_message(db: Database) -> None:
    await db.add_reminder(
        ReminderCreate(message="Fajr (Subuh) Prayer", trigger_time="2026-09-07T05:00:00")
    )
    await db.add_reminder(
        ReminderCreate(message="Wash motorcycle", trigger_time="2026-09-06T10:00:00")
    )
    result = await get_reminders(query="pray")
    assert "Fajr (Subuh) Prayer" in result
    assert "Wash motorcycle" not in result


def test_persona_requires_definitive_absence_answer() -> None:
    assert "answer YES or NO directly" in MAIN_PERSONA
    assert "I'll check" in MAIN_PERSONA
    assert "let me check" in MAIN_PERSONA
    assert "No, there's no prayer reminder set up." in MAIN_PERSONA