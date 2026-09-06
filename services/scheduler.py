"""APScheduler integration for automated jobs — news, agenda, reminders."""

from __future__ import annotations

import logging
import traceback
from datetime import datetime
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


def inject_dependencies(brain, db, send_message_fn) -> None:
    """Inject runtime dependencies for scheduled jobs.

    Args:
        brain: SecondBrain instance for AI processing.
        db: Database instance for data access.
        send_message_fn: Async function to send a Telegram message.
    """
    global _brain, _db, _send_message
    _brain = brain
    _db = db
    _send_message = send_message_fn


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


# ─── Scheduled Jobs ──────────────────────────────────────────────────────────


@alert_on_error
async def morning_news_job() -> None:
    """Fetch, curate, and deliver the morning news digest."""
    logger.info("⏰ Running morning news job...")
    try:
        from services.news import fetch_all_news

        # Fetch raw articles
        raw_articles = await fetch_all_news()

        if "No news articles available" in raw_articles:
            await _send_message("☀️ Good morning! Unfortunately, I couldn't fetch any news today. I'll try again later.")
            return

        # AI-curate the news
        digest = await _brain.curate_news(raw_articles)

        # Save to database
        today = datetime.now(Config.TIMEZONE).strftime("%Y-%m-%d")
        await _db.save_digest(today, digest)

        # Send to user
        header = f"☀️ **Good Morning! Here's your news digest for {today}**\n\n"
        await _send_message(header + digest)

        logger.info("Morning news delivered successfully.")

    except Exception:
        logger.exception("Failed to deliver morning news.")
        await _send_message("⚠️ Sorry, there was an error preparing today's news digest. I'll check into it.")


@alert_on_error
async def daily_agenda_job() -> None:
    """Send the daily agenda — today's tasks and reminders."""
    logger.info("⏰ Running daily agenda job...")
    try:
        today = datetime.now(Config.TIMEZONE).strftime("%Y-%m-%d")
        tasks = await _db.get_today_tasks(today)
        reminders = await _db.get_active_reminders()

        lines = [f"📋 **Daily Agenda — {today}**\n"]

        if tasks:
            lines.append("**📌 Tasks:**")
            for t in tasks:
                priority_icon = {"urgent": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}.get(
                    t.priority.value, ""
                )
                due = f" (due: {t.due_date})" if t.due_date else ""
                lines.append(f"  {priority_icon} #{t.id} {t.description}{due}")
        else:
            lines.append("**📌 Tasks:** All clear! No pending tasks. 🎉")

        if reminders:
            lines.append("\n**⏰ Upcoming Reminders:**")
            for r in reminders:
                lines.append(f"  • {r.message} — {r.trigger_time}")

        lines.append("\nHave a productive day! 💪")
        await _send_message("\n".join(lines))

        logger.info("Daily agenda delivered.")

    except Exception:
        logger.exception("Failed to deliver daily agenda.")


@alert_on_error
async def reminder_check_job() -> None:
    """Check for due reminders and fire them."""
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

    except Exception:
        logger.exception("Error checking reminders.")


@alert_on_error
async def heartbeat_job() -> None:
    """Stamp the heartbeat table so downtime is measurable on restart."""
    if _db is None:
        raise RuntimeError("Database not injected for heartbeat job.")
    await _db.update_heartbeat()


# ─── Phase 3 — data hygiene (nightly) ───────────────────────────────────────


@alert_on_error
async def retention_job() -> None:
    """Nightly (03:00): export old conversations and prune them."""
    if _db is None:
        raise RuntimeError("Database not injected for retention job.")
    from services.export import run_retention

    await run_retention(_db)


@alert_on_error
async def markdown_export_job() -> None:
    """Nightly (03:15): write the Obsidian vault (notes + tasks, incremental)."""
    if _db is None:
        raise RuntimeError("Database not injected for markdown export job.")
    from services.export import run_markdown_export

    await run_markdown_export(_db)


@alert_on_error
async def backup_job() -> None:
    """Nightly (03:30): hot-copy DB + vault to BACKUP_BUCKET (or skip if unset)."""
    if _db is None:
        raise RuntimeError("Database not injected for backup job.")
    from services.export import run_backup

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


# ─── Scheduler Setup ─────────────────────────────────────────────────────────


def create_scheduler() -> AsyncIOScheduler:
    """Create and configure the AsyncIOScheduler with persistent job store.

    Returns:
        Configured but not-yet-started scheduler.
    """
    db_url = f"sqlite:///{Config.DATABASE_PATH.parent / 'scheduler_jobs.db'}"

    jobstores = {
        "default": SQLAlchemyJobStore(url=db_url),
    }

    scheduler = AsyncIOScheduler(
        jobstores=jobstores,
        timezone=Config.TIMEZONE_STR,
    )

    # Morning news — daily at configured time
    scheduler.add_job(
        morning_news_job,
        trigger="cron",
        hour=Config.NEWS_DELIVERY_HOUR,
        minute=Config.NEWS_DELIVERY_MINUTE,
        id="morning_news",
        name="Morning News Digest",
        replace_existing=True,
        misfire_grace_time=3600,  # Allow up to 1 hour late
    )

    # Daily agenda — daily at configured time
    scheduler.add_job(
        daily_agenda_job,
        trigger="cron",
        hour=Config.AGENDA_DELIVERY_HOUR,
        minute=Config.AGENDA_DELIVERY_MINUTE,
        id="daily_agenda",
        name="Daily Agenda",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # Reminder checker — every minute
    scheduler.add_job(
        reminder_check_job,
        trigger="interval",
        minutes=1,
        id="reminder_check",
        name="Reminder Checker",
        replace_existing=True,
    )

    # Heartbeat — every 5 minutes
    scheduler.add_job(
        heartbeat_job,
        trigger="interval",
        minutes=5,
        id="heartbeat",
        name="Heartbeat",
        replace_existing=True,
    )

    # Phase 3 — data hygiene, nightly (Asia/Jakarta)
    scheduler.add_job(
        retention_job,
        trigger="cron",
        hour=3, minute=0,
        id="retention",
        name="Conversation Retention (export + prune)",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        markdown_export_job,
        trigger="cron",
        hour=3, minute=15,
        id="markdown_export",
        name="Markdown Vault Export",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        backup_job,
        trigger="cron",
        hour=3, minute=30,
        id="backup",
        name="GCS Backup",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    logger.info(
        "Scheduler configured: news at %02d:%02d, agenda at %02d:%02d (%s)",
        Config.NEWS_DELIVERY_HOUR,
        Config.NEWS_DELIVERY_MINUTE,
        Config.AGENDA_DELIVERY_HOUR,
        Config.AGENDA_DELIVERY_MINUTE,
        Config.TIMEZONE_STR,
    )

    return scheduler
