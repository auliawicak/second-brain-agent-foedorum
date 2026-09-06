"""Unit tests for Phase 2 — feedback & corrections."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.parsing import extract_json_object
from bot.feedback import (
    decode_feedback_cb,
    detect_edit_request,
    encode_feedback_cb,
    parse_created_id,
    should_attach_feedback,
)
from storage.database import Database


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "test.db"))
    await db.connect()
    yield db
    await db.close()


# ─── storage ────────────────────────────────────────────────────────────────


async def test_corrections_add_and_count(db: Database) -> None:
    assert await db.get_correction_counts(days=7) == 0
    rid = await db.add_correction(
        trigger="explicit",
        user_message="that was wrong",
        agent_action="list_tasks -> ...",
        correction="should have sorted by priority",
    )
    assert rid > 0
    await db.add_correction(trigger="edit", user_message="delete task 3", correction="x")
    assert await db.get_correction_counts(days=7) == 2
    assert await db.get_correction_counts(days=1) == 2


async def test_feedback_add_and_counts(db: Database) -> None:
    up, down = await db.get_feedback_counts(days=7)
    assert (up, down) == (0, 0)
    await db.add_feedback("1:5", 1, model_id="muse", tier="chat")
    await db.add_feedback("1:6", -1, model_id="muse", tier="chat", note="dup")
    up, down = await db.get_feedback_counts(days=7)
    assert (up, down) == (1, 1)


# ─── parsing ────────────────────────────────────────────────────────────────


def test_extract_json_object_variants() -> None:
    assert extract_json_object('{"is_correction": true, "what_was_wrong": "nope"}') == {
        "is_correction": True,
        "what_was_wrong": "nope",
    }
    assert extract_json_object(
        'Sure thing ```json {"is_correction": false}``` done'
    ) == {"is_correction": False}
    assert extract_json_object("no structured reply here") is None
    # repair target: trailing comma
    assert extract_json_object('{"is_correction": true, "what_was_wrong": "a,b",}') == {
        "is_correction": True,
        "what_was_wrong": "a,b",
    }


# ─── feedback helpers ───────────────────────────────────────────────────────


def test_should_attach_feedback() -> None:
    assert should_attach_feedback("short", True) is True
    assert should_attach_feedback("short", False) is False
    assert should_attach_feedback("x" * 250, False) is True
    assert should_attach_feedback("x" * 200, False) is False


def test_feedback_cb_roundtrip() -> None:
    data = encode_feedback_cb(1, "AbCdE_FGh1")
    assert decode_feedback_cb(data) == {"rating": 1, "token": "AbCdE_FGh1"}
    assert decode_feedback_cb(encode_feedback_cb(-1, "zz"))["rating"] == -1
    assert decode_feedback_cb("fb|1|") is None
    assert decode_feedback_cb("nope|1|xx") is None
    assert decode_feedback_cb("fb|0|xx") is None


def test_parse_created_id() -> None:
    assert parse_created_id("✅ Task #12 created: buy milk [high]") == 12
    assert parse_created_id("✅ Note #7 saved") == 7
    assert parse_created_id("no id here") is None


def test_detect_edit_request() -> None:
    assert detect_edit_request("please delete task 5") == 5
    assert detect_edit_request("complete #12") == 12
    assert detect_edit_request("DONE task #3 please") == 3
    assert detect_edit_request("what's the weather") is None
    assert detect_edit_request("update my task to tomorrow") is None  # no id


# ─── correction classifier ──────────────────────────────────────────────────


async def test_chat_returns_chatresult(db: Database, monkeypatch) -> None:
    from agent.brain import ChatResult, SecondBrain

    brain = SecondBrain(db)
    await brain.start()

    calls = {"n": 0}

    async def fake_generate(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return '{"tool": "add_task", "args": {"description": "walk dog", "priority": "low"}}'
        return "Added the task."

    monkeypatch.setattr(SecondBrain, "_generate", fake_generate)

    result = await brain.chat("please add walk dog task", confirmed=True)
    assert isinstance(result, ChatResult)
    assert result.executed_tool is True
    assert result.tool == "add_task"
    assert result.tool_args == {"description": "walk dog", "priority": "low"}
    assert "Task #" in (result.tool_result or "")
    assert "Added the task." in result.text
    assert result.model_id is None  # no real provider involved

    # last_chat is exposed for the feedback layer.
    assert brain.last_chat is result
    await brain.stop()


async def test_classify_correction_parses(monkeypatch) -> None:
    from agent.brain import SecondBrain

    async def fake_generate(self, *args, **kwargs):
        return '{"is_correction": true, "what_was_wrong": "used the wrong due date"}'

    brain = SecondBrain.__new__(SecondBrain)  # skip DB init for this test
    brain._generate = fake_generate.__get__(brain, SecondBrain)

    result = await brain.classify_correction("wrong, it was due friday", "add_task")
    assert result == {
        "is_correction": True,
        "what_was_wrong": "used the wrong due date",
    }


async def test_classify_correction_non_correction(monkeypatch) -> None:
    from agent.brain import SecondBrain

    async def fake_generate(self, *args, **kwargs):
        return '{"is_correction": false, "what_was_wrong": null}'

    brain = SecondBrain.__new__(SecondBrain)
    brain._generate = fake_generate.__get__(brain, SecondBrain)

    assert await brain.classify_correction("ok thanks!", "complete_task") == {
        "is_correction": False,
        "what_was_wrong": None,
    }


# ─── handler feedback flow (lightweight fakes, no Telegram API) ─────────────


async def test_thumbs_up_callback_stores_feedback(db: Database, monkeypatch) -> None:
    from bot.telegram_handler import TelegramBot
    from config import Config

    monkeypatch.setattr(Config, "TELEGRAM_USER_ID", 1)

    class FakeUser:
        id = 1

    class FakeCb:
        def __init__(self, data):
            self.data = data
            self._msg = type("M", (), {
                "chat": type("C", (), {"id": 42})(),
                "message_id": 7,
            })()
            self.answered: list[str | None] = []

        @property
        def message(self):
            return self._msg

        async def answer(self, text=None, **kw):
            self.answered.append(text)

    class FakeUpdate:
        effective_user = FakeUser()
        callback_query = FakeCb(encode_feedback_cb(1, "tok"))
        message = None

    bot = TelegramBot.__new__(TelegramBot)  # skip __init__ (needs app setup)
    bot.db = db
    bot._msg_meta = {"tok": {"model_id": "muse", "tier": "chat", "agent_action": "x"}}
    bot._pending_thumbsdown = {}

    await bot._handle_callback(FakeUpdate(), None)
    assert await db.get_feedback_counts(days=7) == (1, 0)
    assert FakeUpdate.callback_query.answered == ["Thanks! 👍"]


async def test_thumbs_down_to_correction_flow(db: Database, monkeypatch) -> None:
    from bot.telegram_handler import TelegramBot
    from config import Config

    monkeypatch.setattr(Config, "TELEGRAM_USER_ID", 1)

    class FakeUser:
        id = 1

    class FakeCb:
        def __init__(self, data):
            self.data = data
            self._msg = type("M", (), {
                "chat": type("C", (), {"id": 42})(),
                "message_id": 9,
            })()
            self.answered: list[str | None] = []

        @property
        def message(self):
            return self._msg

        async def answer(self, text=None, **kw):
            self.answered.append(text)

    class FakeUpdate:
        effective_user = FakeUser()
        callback_query = FakeCb(encode_feedback_cb(-1, "tok"))
        message = None

    bot = TelegramBot.__new__(TelegramBot)
    bot.db = db
    bot._msg_meta = {"tok": {"model_id": "muse", "tier": "chat", "agent_action": "add_task"}}
    bot._pending_thumbsdown = {}

    await bot._handle_callback(FakeUpdate(), None)
    assert await db.get_feedback_counts(days=7) == (0, 1)
    assert bot._pending_thumbsdown.get(42) is not None

    # User replies with the correction text → stored as a correction.
    class FakeMsg:
        async def reply_text(self, text, **kw):
            self.sent = text

    class FakeTextUpdate:
        effective_user = FakeUser()
        message = FakeMsg()
        text = "you should have set the due date to friday also"

    consumed = await bot._capture_thumbsdown_reply(42, "you should have set the due date to friday also", FakeTextUpdate())
    assert consumed is True
    assert 42 not in bot._pending_thumbsdown
    assert await db.get_correction_counts(days=7) == 1