"""APScheduler integration for automated jobs — brief, close-out, triggers.

Phase 7 consolidates every scheduled message into a small set of jobs:

  1. morning_brief   — ONE 06:00 brief (tasks/reminders/overdue + 1 news item)
  2. evening_closeout— ONE 21:00 "what got done today?" message
  3. weekly_review   — Friday 17:00 (completed vs slipped + one pattern)
  4. condition_checks— every 15 min (nudges, deduped once/day/entity)
  5. reminder_check  — every minute (also stamps the heartbeat)
  6. maintenance     — nightly 03:00 (retention + markdown export + backup)
  7. nightly_consolidation — 00:15 (Phase 6 learning loop)
  8. persona_proposal — 1st of month 04:00 (Phase 6 operating principles)

The persistent job store is purged on every boot so stale jobs from earlier
phases (the old separate news/agenda/hygiene jobs) can never fire again.
"""

from __future__ import annotations

import logging
import traceback
from datetime import datetime, timedelta
from functools import wraps
from typing import Any, Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

from config import Config
from services.alerts import alert_owner

logger = logging.getLogger(__name__)

# Type hints for injected dependencies (set at runtime)
_brain = None  # agent.brain.SecondBrain
_db = None  # storage.database.Database
_send_message = None  # async callable(str) -> None


def inject_dependencies(brain, db, send_message_fn, proposal_sender=None) -> None:
    """Inject runtime dependencies for scheduled jobs.

    Args:
        brain: SecondBrain instance for AI processing.
        db: Database instance for data access.
        send_message_fn: Async function to send a Telegram message.
        proposal_sender: Optional async sender for Phase 6 proposals
            (wired into services.messaging).
    """
    global _brain, _db, _send_message
    _brain = brain
    _db = db
    _send_message = send_message_fn
    if proposal_sender is not None:
        from services.messaging import set_proposal_sender

        set_proposal_sender(proposal_sender)


def alert_on_error(
    job_func: Callable[..., Awaitable[Any]],
) -> Callable[..., Awaitable[Any]]:
    """Wrap a scheduled job so any uncaught exception alerts the owner."""

    @wraps(job_func)
    async def wrapper(*args, **kwargs):
        try:
            return await job_func(*args, **kwargs)
        except Exception:
            logger.exception("Scheduled job '%s' failed", job_func.__name__)
            await alert_owner(
                f"⚠️ Scheduled job `{job_func.__name__}` failed:\n"
                f"{traceback.format_exc()[-1500:]}",
                dedupe_key=f"job:{job_func.__name__}",
            )

    return wrapper


# ─── Phase 7 — merged morning & evening messages ───────────────────────────


@alert_on_error
async def morning_brief_job() -> None:
    """06:00: send ONE merged brief (tasks + reminders + overdue + 1 news)."""
    if _db is None or _brain is None:
        raise RuntimeError("Morning brief not wired (db/brain not injected).")
    from services.brief import build_morning_brief

    brief = await build_morning_brief(_db, _brain)
    await _send_message(brief)
    logger.info("Morning brief delivered.")


@alert_on_error
async def closeout_job() -> None:
    """21:00: one evening check-in. The reply is logged and can fuel the
    nightly consolidation via the ordinary corrections flow — no nagging."""
    if _db is None:
        raise RuntimeError("Close-out not wired (db not injected).")
    now = datetime.now(Config.TIMEZONE).replace(tzinfo=None)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    created, completed = await _db.get_day_task_stats(
        day_start.isoformat(), day_end.isoformat()
    )

    line = f"🌙 **Evening check-in — {now.strftime('%A')}**\n\nWhat got done today?"
    if completed:
        line += f" You completed {completed} task{'s' if completed != 1 else ''}. 🎉"
    line += "\nAnything I should remember for tomorrow? Just reply."
    await _send_message(line)
    logger.info("Evening close-out delivered (created=%d, completed=%d).", created, completed)


# ─── Reminders + heartbeat ──────────────────────────────────────────────────


@alert_on_error
async def reminder_check_job() -> None:
    """Every minute: fire due reminders, then stamp the heartbeat.

    The heartbeat is folded into this job so the old separate 5-minute job
    can be dropped (Phase 7: total scheduler jobs ≤ 8). Granularity is
    strictly better than before (<1 min instead of 5 min).
    """
    if _db is None:
        raise RuntimeError("Reminder checker not wired (db not injected).")
    try:
        now_iso = datetime.now(Config.TIMEZONE).isoformat()
        due_reminders = await _db.get_due_reminders(now_iso)

        for reminder in due_reminders:
            await _send_message(f"⏰ **Reminder:** {reminder.message}")

            if not reminder.is_recurring:
                await _db.deactivate_reminder(reminder.id)
                logger.info("Reminder #%d fired and deactivated.", reminder.id)
                continue

            # Recurring reminders: advance trigger time to the next occurrence
            next_time = _next_cron_occurrence(reminder.cron_expression) if reminder.cron_expression else None
            if next_time:
                await _db.update_reminder_time(reminder.id, next_time)
                logger.info(
                    "Recurring reminder #%d fired, next at %s.", reminder.id, next_time
                )
            else:
                await _db.deactivate_reminder(reminder.id)
                logger.warning(
                    "Recurring reminder #%d deactivated (could not compute next time).",
                    reminder.id,
                )

        try:
            await _db.update_heartbeat()
        except Exception:
            logger.exception("Heartbeat stamp failed.")
    except Exception:
        logger.exception("Error checking reminders.")


# ─── Phase 7 — condition checks (every 15 min) ─────────────────────────────


@alert_on_error
async def condition_checks_job() -> None:
    """15-minute pass: fire condition nudges (deduped once per day per entity).

    Quiet runs send nothing; a run that fires groups everything into a single
    message (§7.4).
    """
    if _db is None:
        raise RuntimeError("Condition checks not wired (db not injected).")
    from services.triggers import run_condition_checks

    lines = await run_condition_checks(_db)
    if not lines:
        return
    body = "\n\n".join(lines)
    if len(lines) > 1:
        body = "🧠 **A few things need your attention:**\n\n" + body
    await _send_message(body)


# ─── Phase 3 + maintenance (one nightly job) ───────────────────────────────


@alert_on_error
async def maintenance_job() -> None:
    """Nightly 03:00: retention prune → markdown vault export → GCS backup."""
    if _db is None:
        raise RuntimeError("Maintenance not wired (db not injected).")
    from services.export import run_backup, run_markdown_export, run_retention

    await run_retention(_db)
    await run_markdown_export(_db)
    result = await run_backup(_db)
    if result.get("skipped"):
        logger.info("Backup skipped (BACKUP_BUCKET unset).")


async def check_downtime_on_startup(db) -> None:
    """Alert the owner if the agent appears to have been down > 2 hours."""
    last_seen = await db.get_heartbeat()
    if not last_seen:
        logger.info("No previous heartbeat — first boot.")
        return

    try:
        last = datetime.fromisoformat(last_seen)
        now = datetime.now()
        down = now - last
        if down.total_seconds() > 2 * 3600:
            hours, remainder = divmod(int(down.total_seconds()), 3600)
            minutes = remainder // 60
            await alert_owner(f"⏳ Agent was down for {hours}h {minutes}m.")
    except Exception:
        logger.exception("Failed to compute downtime from heartbeat %r", last_seen)


def _next_cron_occurrence(cron_expression: str) -> str | None:
    """Compute the next trigger time for a cron expression.

    Returns a naive local (Asia/Jakarta) ISO datetime string, or None if the
    expression is invalid.
    """
    try:
        from apscheduler.triggers.cron import CronTrigger

        trigger = CronTrigger.from_crontab(cron_expression, timezone=Config.TIMEZONE)
        next_fire = trigger.get_next_fire_time(None, datetime.now(Config.TIMEZONE))
        if next_fire is None:
            return None
        return next_fire.replace(tzinfo=None).isoformat(timespec="seconds")
    except Exception:
        logger.exception("Failed to compute next occurrence for cron: %s", cron_expression)
        return None


# ─── Phase 6 — learning loop (nightly / weekly / monthly) ───────────────────


@alert_on_error
async def consolidation_job() -> None:
    """Nightly (00:15): consolidate the day's corrections into preferences."""
    if _db is None or _brain is None:
        raise RuntimeError("Learning loop not wired (db/brain not injected).")
    from services.consolidation import consolidate_nightly

    summary = await consolidate_nightly(_db, _brain)
    analyzed = summary.get("analyzed", 0)
    if summary:
        logger.info(
            "Nightly consolidation: %d episodes → %d applied, %d superseded, %d forgotten.",
            analyzed,
            len(summary.get("applied", [])),
            len(summary.get("superseded", [])),
            len(summary.get("forgotten", [])),
        )
    else:
        logger.info("Nightly consolidation: nothing to consolidate (idle).")


@alert_on_error
async def weekly_review_job() -> None:
    """Friday 17:00 (§7.3): completed vs slipped + one observed pattern."""
    if _db is None or _brain is None:
        raise RuntimeError("Weekly review not wired.")
    if _send_message is None:
        raise RuntimeError("Message sender not wired for weekly review.")
    from services.consolidation import weekly_conversation_review

    review = await weekly_conversation_review(_db, _brain)
    if not review:
        logger.info("Weekly review: nothing to summarize this week.")
        return
    await _send_message(f"📈 **Weekly review**\n\n{review}")


@alert_on_error
async def persona_proposal_job() -> None:
    """Monthly (1st, 04:00): draft a candidate operating-principles update
    and send it to the owner as an interactive proposal (§6.5)."""
    if _db is None or _brain is None:
        raise RuntimeError("Persona proposal not wired.")
    from services.persona import build_persona_proposal

    result = await build_persona_proposal(_db, _brain)
    logger.info("Persona proposal job finished (proposed=%r...)", (result or "")[:40])


# ─── Scheduler Setup ─────────────────────────────────────────────────────────


def create_scheduler(jobstore_url: str | None = None) -> AsyncIOScheduler:
    """Create and configure the AsyncIOScheduler with persistent job store.

    Registers exactly 8 jobs. The store is purged on every boot so stale
    persisted jobs from earlier phases (separate news/agenda/heartbeat and
    the three individual hygiene jobs) can never fire again — Phase 7
    acceptance requires exactly one scheduled message at 06:00 and ≤8 jobs.

    Args:
        jobstore_url: Override the SQLAlchemy store URL (tests pass an
            in-memory "sqlite://" to avoid touching the real store file).

    Returns:
        Configured but not-yet-started scheduler.
    """
    db_url = (
        jobstore_url
        or f"sqlite:///{Config.DATABASE_PATH.parent / 'scheduler_jobs.db'}"
    )

    scheduler = AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=db_url)},
        timezone=Config.TIMEZONE_STR,
    )

    scheduler.remove_all_jobs()  # boot-fresh job set (see docstring)

    # 1. Morning brief — ONE merged 06:00 message (§7.1)
    scheduler.add_job(
        morning_brief_job,
        trigger="cron",
        hour=Config.BRIEF_HOUR,
        minute=Config.BRIEF_MINUTE,
        id="morning_brief",
        name="Morning Brief (unified)",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # 2. Evening close-out (§7.2)
    scheduler.add_job(
        closeout_job,
        trigger="cron",
        hour=Config.CLOSEOUT_HOUR,
        minute=Config.CLOSEOUT_MINUTE,
        id="evening_closeout",
        name="Evening Close-out",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # 3. Weekly review — Friday deep-tier review (§7.3)
    scheduler.add_job(
        weekly_review_job,
        trigger="cron",
        day_of_week=Config.REVIEW_DAY,
        hour=Config.REVIEW_HOUR,
        minute=Config.REVIEW_MINUTE,
        id="weekly_review",
        name="Weekly Review (completed vs slipped)",
        replace_existing=True,
        misfire_grace_time=7200,
    )

    # 4. Condition checks — one every-15-minutes pass (§7.4)
    scheduler.add_job(
        condition_checks_job,
        trigger="interval",
        minutes=15,
        id="condition_checks",
        name="Condition Checks",
        replace_existing=True,
    )

    # 5. Reminder checker — every minute (heartbeat folded in)
    scheduler.add_job(
        reminder_check_job,
        trigger="interval",
        minutes=1,
        id="reminder_check",
        name="Reminder Checker + Heartbeat",
        replace_existing=True,
    )

    # 6. Nightly maintenance — retention, markdown export, backup (03:00)
    scheduler.add_job(
        maintenance_job,
        trigger="cron",
        hour=3, minute=0,
        id="maintenance",
        name="Nightly Maintenance (retention + export + backup)",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # 7. Phase 6 — nightly preference consolidation (00:15)
    scheduler.add_job(
        consolidation_job,
        trigger="cron",
        hour=0, minute=15,
        id="nightly_consolidation",
        name="Nightly Preference Consolidation",
        replace_existing=True,
        misfire_grace_time=7200,
    )

    # 8. Phase 6 — monthly operating-principles proposal (1st, 04:00)
    scheduler.add_job(
        persona_proposal_job,
        trigger="cron",
        day=1,
        hour=4, minute=0,
        id="persona_proposal",
        name="Monthly Operating-Principles Proposal",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    logger.info(
        "Scheduler configured: brief %02d:%02d, close-out %02d:%02d, "
        "weekly review %s %02d:%02d, reminders every minute, "
        "condition checks every 15 min (%s)",
        Config.BRIEF_HOUR,
        Config.BRIEF_MINUTE,
        Config.CLOSEOUT_HOUR,
        Config.CLOSEOUT_MINUTE,
        Config.REVIEW_DAY,
        Config.REVIEW_HOUR,
        Config.REVIEW_MINUTE,
        Config.TIMEZONE_STR,
    )

    return scheduler