"""Tests for the Hermes-facing secondbrain CLI bridge."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from secondbrain.cli import _run


def ns(command: str, db: Path, **fields) -> argparse.Namespace:
    base: dict = {"command": command, "db": str(db)}
    base.update(fields)
    return argparse.Namespace(**base)


class TestTasks:
    async def test_roundtrip(self, tmp_path: Path) -> None:
        db = tmp_path / "brain.db"
        add = await _run("tasks", ns("tasks", db, action="add",
                                      description="buy milk", priority="high"))
        assert "Task #1 created" in add

        listing = await _run("tasks", ns("tasks", db, action="list", status="pending",
                                         category=None, date=None))
        assert "#1" in listing and "buy milk" in listing

        missing = await _run("tasks", ns("tasks", db, action="complete", ids=["99"]))
        assert "None of those tasks were found." in missing

        found = await _run("tasks", ns("tasks", db, action="complete", ids=["1"]))
        assert "Task #1 completed" in found

        empty = await _run("tasks", ns("tasks", db, action="list", status="pending",
                                       category=None, date=None))
        assert "No tasks found" in empty

    async def test_agenda(self, tmp_path: Path) -> None:
        db = tmp_path / "brain.db"
        await _run("tasks", ns("tasks", db, action="add",
                               description="standup prep", category="work"))
        agenda = await _run("agenda", ns("agenda", db))
        assert "Agenda" in agenda
        assert "standup prep" in agenda


class TestNotes:
    async def test_roundtrip(self, tmp_path: Path) -> None:
        db = tmp_path / "brain.db"
        saved = await _run("notes", ns("notes", db, action="add",
                                       content="remember the keyword primer",
                                       tags='["proj","pilot"]', category="work"))
        assert "Note #1 saved" in saved

        search = await _run("notes", ns("notes", db, action="search", query="primer"))
        assert "primer" in search
        assert "#proj" in search or "#pilot" in search

        recent = await _run("notes", ns("notes", db, action="recent", limit=5))
        assert "primer" in recent


class TestReminders:
    async def test_set_and_list(self, tmp_path: Path) -> None:
        db = tmp_path / "brain.db"
        set_ok = await _run(
            "reminders",
            ns("reminders", db, action="set", message="daily standup",
               trigger_time="2026-09-08T09:00:00", recurring=True,
               cron_expression="0 9 * * 1-5"),
        )
        assert "Reminder #1 set" in set_ok
        assert "0 9 * * 1-5" in set_ok

        listing = await _run("reminders", ns("reminders", db, action="list",
                                             all=False, query="standup"))
        assert "daily standup" in listing
        assert "recurring: 0 9 * * 1-5" in listing

        listing_all = await _run("reminders", ns("reminders", db, action="list",
                                                 all=True, query=""))
        assert "daily standup" in listing_all


class TestPrefsAndFacts:
    async def test_prefs(self, tmp_path: Path) -> None:
        db = tmp_path / "brain.db"
        saved = await _run("prefs", ns("prefs", db, action="save",
                                       key="morning_drink", value="black coffee"))
        assert "morning_drink = black coffee" in saved

    async def test_fact(self, tmp_path: Path) -> None:
        db = tmp_path / "brain.db"
        saved = await _run("facts", ns("facts", db, action="remember",
                                       fact="Prefers black coffee at 6am",
                                       category="diet", keywords="coffee morning"))
        assert "preference" in saved


class TestPersona:
    async def test_show_set_history_rollback(self, tmp_path: Path) -> None:
        db = tmp_path / "brain.db"
        show = await _run("persona", ns("persona", db, action="show"))
        assert "Persona v1" in show

        updated = await _run(
            "persona",
            ns("persona", db, action="set", layer="voice",
               text="Speak warmly and briefly."),
        )
        assert "Persona updated" in updated
        assert "v2" in updated

        show = await _run("persona", ns("persona", db, action="show"))
        assert "Speak warmly and briefly." in show

        history = await _run("persona", ns("persona", db, action="history"))
        assert "v2" in history

        rolled = await _run("persona", ns("persona", db, action="rollback", version=1))
        assert "Rolled back to persona v1" in rolled


class TestErrorPaths:
    async def test_no_valid_ids(self, tmp_path: Path) -> None:
        db = tmp_path / "brain.db"
        result = await _run("tasks", ns("tasks", db, action="complete", ids=[]))
        assert "No valid task IDs given." in result