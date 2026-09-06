"""Unit tests for Phase 7 — unified briefs and condition triggers.

Covers:
- §7.1 merged morning brief (one message, one news item) + empty-brief news
- §7.2 evening close-out sends exactly one message
- §7.3 weekly review (completed vs slipped + one pattern, single deep call)
- §7.4 condition checks: overdue/untouched tasks, capture/execute imbalance,
  stale notes matching an active topic — with once-per-day-per-entity dedupe
- Acceptance: ≤8 scheduler jobs, exactly one scheduled 06:00 cron message,
  condition pass <2s at 1,000 tasks (accounts do not exist here yet)
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from apscheduler.triggers.cron import CronTrigger

from config import Config
from services.brief import build_morning_brief
from services.triggers import run_condition_checks
from storage.database import Database
from storage.models import NoteCreate, ReminderCreate, TaskCreate


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "test.db"))
    await db.connect()
    yield db
    await db.close()


def _now_local() -> datetime:
    return datetime.now(Config.TIMEZONE).replace(tzinfo=None)


# ─── scheduler acceptance — ≤8 jobs, one scheduled 06:00 message ───────────


def test_scheduler_registers_exactly_eight_jobs(tmp_path: Path) -> None:
    from services.scheduler import create_scheduler

    sched = create_scheduler(jobstore_url=f"sqlite:///{tmp_path / 'jobs.db'}")
    try:
        jobs = sched.get_jobs()
        ids = {j.id for j in jobs}

        assert len(jobs) == 8
        assert ids == {
            "morning_brief",
            "evening_closeout",
            "weekly_review",
            "condition_checks",
            "reminder_check",
            "maintenance",
            "nightly_consolidation",
            "persona_proposal",
        }
        # the phase-0/3 single-purpose jobs are gone (no second 06:00 message)
        assert not (ids & {
            "morning_news", "daily_agenda", "heartbeat",
            "retention", "markdown_export", "backup",
        })

        now = datetime.now(Config.TIMEZONE)

        # exactly ONE cron job fires at the brief time (06:00 by default)
        at_brief_time = []
        for job in jobs:
            if not isinstance(job.trigger, CronTrigger):
                continue
            nxt = job.trigger.get_next_fire_time(None, now)
            if (
                nxt is not None
                and nxt.hour == Config.BRIEF_HOUR
                and nxt.minute == Config.BRIEF_MINUTE
            ):
                at_brief_time.append(job.id)
        assert at_brief_time == ["morning_brief"]

        # close-out fires at its configured hour; weekly review on Friday
        close = sched.get_job("evening_closeout").trigger.get_next_fire_time(None, now)
        assert close.hour == Config.CLOSEOUT_HOUR
        review = sched.get_job("weekly_review").trigger.get_next_fire_time(None, now)
        assert review.hour == Config.REVIEW_HOUR
    finally:
        if sched.state:  # 0 = stopped/never started; only shutdown a running one
            sched.shutdown(wait=False)


# ─── §7.1 morning brief ────────────────────────────────────────────────────


class _NoNews:
    async def curate_single_news_item(self, raw_articles: str, context: str = ""):
        return None


async def _no_news_fetcher() -> str:
    return "No news articles available at this time."


async def test_morning_brief_is_one_message_with_tasks_and_no_news(
    db: Database, monkeypatch
) -> None:
    import services.brief as brief_mod

    await db.add_task(TaskCreate(description="finish the phase 7 report"))
    await db.add_reminder(
        ReminderCreate(
            message="standup at 9:30",
            trigger_time=_now_local().isoformat(),
        )
    )
    monkeypatch.setattr(brief_mod, "fetch_all_news", _no_news_fetcher)

    text = await build_morning_brief(db, _NoNews())

    assert "Good Morning" in text
    assert "finish the phase 7 report" in text
    assert "standup at 9:30" in text  # reminders merged into the one brief
    assert "**📰 One thing worth reading:**" not in text  # news down → no block
    # it is exactly one deliverable message body
    assert text.count("**Good Morning") == 1

    # digest persisted for auditability
    today = datetime.now(Config.TIMEZONE).strftime("%Y-%m-%d")
    assert today in (await db.get_digest(today) or "")


async def test_morning_brief_include_single_news_item(
    db: Database, monkeypatch
) -> None:
    import services.brief as brief_mod

    class _Stub:
        async def curate_single_news_item(self, raw_articles: str, context: str = ""):
            return {"headline": "Headline One", "summary": "Sum", "why": "Why it matters", "url": "https://ex"}

    async def _news_fetcher() -> str:
        return "Title: Headline One\nSource: S\nCategory: technology\nDescription: D\nURL: https://ex"

    monkeypatch.setattr(brief_mod, "fetch_all_news", _news_fetcher)

    text = await build_morning_brief(db, _Stub())

    assert "**📰 One thing worth reading:**" in text
    assert "Headline One" in text
    assert "Why it matters" in text
    # still exactly ONE news item, not a multi-headline digest
    assert text.count("**📰 One thing worth reading:**") == 1


# ─── §7.2 evening close-out ────────────────────────────────────────────────


async def test_closeout_sends_exactly_one_message(db: Database) -> None:
    from services import scheduler as sched_mod

    sent: list[str] = []

    async def record(msg: str) -> None:
        sent.append(msg)

    sched_mod.inject_dependencies(None, db, record, None)
    try:
        await sched_mod.closeout_job()
        assert len(sent) == 1
        assert "What got done today?" in sent[0]
    finally:
        sched_mod.inject_dependencies(None, None, None)


async def test_closeout_mentions_completed_count(db: Database) -> None:
    from services import scheduler as sched_mod

    sent: list[str] = []

    async def record(msg: str) -> None:
        sent.append(msg)

    t = await db.add_task(TaskCreate(description="report"))
    await db.complete_task(t.id)
    sched_mod.inject_dependencies(None, db, record, None)
    try:
        await sched_mod.closeout_job()
        assert len(sent) == 1
        assert "completed 1 task" in sent[0]
    finally:
        sched_mod.inject_dependencies(None, None, None)


# ─── §7.3 weekly review ────────────────────────────────────────────────────


class _ReviewStub:
    def __init__(self) -> None:
        self.calls = 0

    async def _generate(self, *args, **kwargs) -> str:
        self.calls += 1
        return "Done: shipped v2. Slipped: migrate db. Pattern: dependencies bite again."


async def test_weekly_review_reports_completed_vs_slipped(db: Database) -> None:
    from services.consolidation import weekly_conversation_review

    done = await db.add_task(TaskCreate(description="ship v2"))
    old = (_now_local() - timedelta(days=3)).isoformat()
    await db.db.execute(
        "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
        (old, done.id),
    )
    await db.db.commit()

    due = (datetime.now(Config.TIMEZONE) - timedelta(days=1)).strftime("%Y-%m-%d")
    await db.add_task(TaskCreate(description="migrate db", due_date=due))
    await db.log_conversation("user", "hit blockers on the migration")

    stub = _ReviewStub()
    text = await weekly_conversation_review(db, stub)

    assert stub.calls == 1  # single deep-tier call
    assert text and "Done: shipped v2" in text
    assert "Slipped: migrate db" in text


async def test_weekly_review_idles_when_nothing_happened(db: Database) -> None:
    from services.consolidation import weekly_conversation_review

    class _Raises:
        async def _generate(self, *args, **kwargs):
            raise AssertionError("should not call the model when idle")

    assert await weekly_conversation_review(db, _Raises()) is None


# ─── §7.4 overdue tasks ────────────────────────────────────────────────────


async def test_overdue_task_nudged_once_per_day(db: Database) -> None:
    due = (datetime.now(Config.TIMEZONE) - timedelta(days=2)).strftime("%Y-%m-%d")
    await db.add_task(TaskCreate(description="pay the tax bill", due_date=due))

    first = await run_condition_checks(db)
    assert any("pay the tax bill" in line for line in first)

    # a second pass the same day must NOT fire the same nudge again
    assert await run_condition_checks(db) == []


async def test_recently_overdue_task_not_nudged(db: Database) -> None:
    # due today → within the 24h grace window → untouched by the trigger
    due = datetime.now(Config.TIMEZONE).strftime("%Y-%m-%d")
    await db.add_task(TaskCreate(description="due today, fine", due_date=due))

    lines = await run_condition_checks(db)
    assert not any("due today, fine" in line for line in lines)


# ─── §7.4 capture/execute imbalance ────────────────────────────────────────


async def test_imbalance_fires_and_dedupes(db: Database) -> None:
    for i in range(6):
        await db.add_task(TaskCreate(description=f"captured task {i}"))

    lines = await run_condition_checks(db)
    assert any("captured 6 tasks" in line for line in lines)
    assert await run_condition_checks(db) == []


async def test_imbalance_cleared_when_something_finished(db: Database) -> None:
    for i in range(6):
        await db.add_task(TaskCreate(description=f"mixed task {i}"))
    tasks = await db.get_tasks()
    await db.complete_task(tasks[0].id)

    lines = await run_condition_checks(db)
    assert not any("You've captured" in line for line in lines)


async def test_imbalance_quiet_below_threshold(db: Database) -> None:
    for i in range(5):
        await db.add_task(TaskCreate(description=f"light task {i}"))

    lines = await run_condition_checks(db)
    assert not any("You've captured" in line for line in lines)


# ─── §7.4 stale note matching an active topic ──────────────────────────────


async def test_stale_note_surfaced_once(db: Database) -> None:
    await db.add_task(TaskCreate(description="quarterly tax filing"))
    note = await db.add_note(
        NoteCreate(content="tax filing checklist and deadlines", category="finance")
    )
    old = (_now_local() - timedelta(days=90)).isoformat()
    await db.db.execute("UPDATE notes SET created_at = ? WHERE id = ?", (old, note.id))
    await db.db.commit()

    lines = await run_condition_checks(db)
    assert any("tax filing checklist" in line for line in lines)
    assert await run_condition_checks(db) == []


async def test_fresh_note_never_surfaced(db: Database) -> None:
    await db.add_task(TaskCreate(description="website redesign"))
    await db.add_note(NoteCreate(content="website redesign mockup ideas"))

    lines = await run_condition_checks(db)
    assert not any("mockup ideas" in line for line in lines)


async def test_no_active_topic_means_no_stale_note(db: Database) -> None:
    # no active tasks → no active-topic signal → the note is not surfaced even
    # though it is old (avoids nagging about unrelated history)
    note = await db.add_note(NoteCreate(content="old unrelated rambling"))
    old = (_now_local() - timedelta(days=200)).isoformat()
    await db.db.execute("UPDATE notes SET created_at = ? WHERE id = ?", (old, note.id))
    await db.db.commit()

    lines = await run_condition_checks(db)
    assert not any("old unrelated rambling" in line for line in lines)


# ─── §7.4 acceptance — <2s at 1,000 tasks ──────────────────────────────────


async def test_condition_checks_under_2_seconds_at_1000_tasks(db: Database) -> None:
    now = _now_local()
    rows = []
    for i in range(1000):
        rows.append(
            (
                f"task number {i}",
                "in_progress" if i % 3 == 0 else "pending",
                "medium",
                (now - timedelta(days=i % 40)).strftime("%Y-%m-%d"),
                "general",
                (now - timedelta(days=i % 10)).isoformat(),
            )
        )
    await db.db.executemany(
        """INSERT INTO tasks (description, status, priority, due_date, category, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        rows,
    )
    await db.db.commit()

    t0 = time.monotonic()
    lines = await run_condition_checks(db)
    elapsed = time.monotonic() - t0

    assert lines  # overdue + imbalance both fire at this scale
    assert elapsed < 2.0
    # no entity fires twice within this single pass either
    assert len(lines) <= 6