"""Second Brain Agent — Main Entry Point.

Initializes and runs all components:
1. SQLite Database
2. Antigravity AI Agents (fast, deep, news)
3. APScheduler (news, agenda, reminders)
4. Telegram Bot (long polling)
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from config import Config


def setup_logging() -> None:
    """Configure structured logging."""
    logging.basicConfig(
        level=getattr(logging, Config.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)-25s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
        ],
    )
    # Quiet down noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.INFO)


async def main() -> None:
    """Main async entry point — starts all services and runs the bot."""
    setup_logging()
    logger = logging.getLogger("main")

    # ── Validate Config ───────────────────────────────────────────────────
    errors = Config.validate()
    if errors:
        for err in errors:
            logger.error("Config error: %s", err)
        logger.error(
            "Please create a .env file from .env.example and fill in the required values."
        )
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("🧠 Second Brain Agent starting...")
    logger.info("=" * 60)

    # ── Initialize Database ───────────────────────────────────────────────
    from storage.database import Database

    db = Database(Config.DATABASE_PATH)
    await db.connect()
    logger.info("✅ Database ready: %s", Config.DATABASE_PATH)

    # ── Initialize AI Agents ──────────────────────────────────────────────
    from agent.brain import SecondBrain

    brain = SecondBrain(db)
    await brain.start()
    logger.info("✅ AI Agents ready (fast: %s, deep: %s)", Config.FAST_MODEL, Config.DEEP_MODEL)

    # ── Initialize Telegram Bot ───────────────────────────────────────────
    from bot.telegram_handler import TelegramBot

    telegram_bot = TelegramBot(brain, db)
    app = telegram_bot.build()
    logger.info("✅ Telegram bot built.")

    # ── Initialize Scheduler ──────────────────────────────────────────────
    from services.scheduler import create_scheduler, inject_dependencies

    inject_dependencies(brain, db, telegram_bot.send_message)
    scheduler = create_scheduler()
    logger.info("✅ Scheduler configured.")

    # ── Start Everything ──────────────────────────────────────────────────
    try:
        # Initialize the Telegram application
        await app.initialize()
        await app.start()

        # Set bot command menu
        await telegram_bot.set_bot_commands()

        # Start the scheduler
        scheduler.start()
        logger.info("✅ Scheduler started.")

        # Start polling (this blocks until stopped)
        logger.info("✅ Starting Telegram polling...")
        logger.info("=" * 60)
        logger.info("🧠 Second Brain is LIVE! Send me a message on Telegram.")
        logger.info("=" * 60)

        # Start polling for updates
        await app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query"],
        )

        # Keep running until interrupted
        stop_event = asyncio.Event()

        def _signal_handler(sig, frame):
            logger.info("Received signal %s, shutting down...", sig)
            stop_event.set()

        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)

        await stop_event.wait()

    except Exception:
        logger.exception("Fatal error in main loop.")
    finally:
        # ── Graceful Shutdown ─────────────────────────────────────────────
        logger.info("Shutting down...")

        try:
            scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped.")
        except Exception:
            pass

        try:
            if app.updater and app.updater.running:
                await app.updater.stop()
            await app.stop()
            await app.shutdown()
            logger.info("Telegram bot stopped.")
        except Exception:
            pass

        try:
            await brain.stop()
            logger.info("AI agents stopped.")
        except Exception:
            pass

        try:
            await db.close()
            logger.info("Database closed.")
        except Exception:
            pass

        logger.info("🧠 Second Brain shut down cleanly. Goodbye!")


if __name__ == "__main__":
    asyncio.run(main())
