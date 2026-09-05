"""Telegram bot handler — commands, free-text routing, and application setup."""

from __future__ import annotations

import logging
from datetime import datetime

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from agent.brain import SecondBrain
from bot.formatters import format_help_message, split_long_message
from bot.middleware import authorized_only, error_handler
from config import Config
from services.news import fetch_all_news
from storage.database import Database

logger = logging.getLogger(__name__)


class TelegramBot:
    """Telegram bot interface for the Second Brain.

    Registers all command handlers and routes free-text messages
    to the Antigravity agent.
    """

    def __init__(self, brain: SecondBrain, db: Database) -> None:
        self.brain = brain
        self.db = db
        self.app: Application | None = None

    def build(self) -> Application:
        """Build the Telegram application with all handlers."""
        self.app = (
            ApplicationBuilder()
            .token(Config.TELEGRAM_BOT_TOKEN)
            .connect_timeout(30.0)
            .read_timeout(30.0)
            .write_timeout(30.0)
            .get_updates_read_timeout(30)
            .get_updates_connect_timeout(15)
            .job_queue(None)  # critical: no second scheduler (APScheduler is in-process)
            .build()
        )

        # Register command handlers
        commands = [
            ("start", self._cmd_start),
            ("help", self._cmd_help),
            ("tasks", self._cmd_tasks),
            ("addtask", self._cmd_addtask),
            ("done", self._cmd_done),
            ("notes", self._cmd_notes),
            ("save", self._cmd_save),
            ("search", self._cmd_search),
            ("news", self._cmd_news),
            ("daily", self._cmd_daily),
            ("remind", self._cmd_remind),
            ("think", self._cmd_think),
            ("status", self._cmd_status),
        ]

        for name, handler in commands:
            self.app.add_handler(CommandHandler(name, handler))

        # Free-text message handler (must be added last)
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )

        # Global error handler
        self.app.add_error_handler(self._global_error_handler)

        return self.app

    async def set_bot_commands(self) -> None:
        """Set the bot's command menu in Telegram."""
        if not self.app:
            return

        await self.app.bot.set_my_commands([
            BotCommand("daily", "📅 Today's agenda"),
            BotCommand("tasks", "📋 View tasks"),
            BotCommand("addtask", "➕ Add a task"),
            BotCommand("done", "✅ Complete a task"),
            BotCommand("notes", "📝 Recent notes"),
            BotCommand("save", "💾 Save a note"),
            BotCommand("search", "🔍 Search notes"),
            BotCommand("news", "📰 Latest news digest"),
            BotCommand("remind", "⏰ Set a reminder"),
            BotCommand("think", "🧠 Deep reasoning mode"),
            BotCommand("help", "❓ Show all commands"),
            BotCommand("status", "ℹ️ System status"),
        ])

    async def send_message(self, text: str) -> None:
        """Send a message to the authorized user (used by scheduler).

        Handles long messages by splitting them.
        """
        if not self.app:
            return

        parts = split_long_message(text)
        for part in parts:
            try:
                await self.app.bot.send_message(
                    chat_id=Config.TELEGRAM_USER_ID,
                    text=part,
                    parse_mode="Markdown",
                )
            except Exception:
                # Fallback: send without formatting if Markdown fails
                try:
                    await self.app.bot.send_message(
                        chat_id=Config.TELEGRAM_USER_ID,
                        text=part,
                    )
                except Exception:
                    logger.exception("Failed to send message to user.")

    # ─── Command Handlers ─────────────────────────────────────────────────

    @authorized_only
    @error_handler
    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command."""
        await update.message.reply_text(
            "🧠 **Welcome to your Second Brain!**\n\n"
            "I'm your personal AI assistant, running 24/7. I can:\n\n"
            "📌 Manage your tasks and to-dos\n"
            "📝 Store your notes and ideas\n"
            "📰 Curate daily news for you\n"
            "⏰ Set reminders and schedules\n"
            "🧠 Help you think through complex problems\n"
            "💬 Chat about anything\n\n"
            "Type /help to see all commands, or just send me a message!",
            parse_mode="Markdown",
        )

    @authorized_only
    @error_handler
    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /help command."""
        await update.message.reply_text(format_help_message(), parse_mode="Markdown")

    @authorized_only
    @error_handler
    async def _cmd_tasks(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /tasks — show pending tasks via the agent."""
        await update.message.reply_text("📋 Fetching your tasks...")
        response = await self.brain.chat(
            "List all my pending tasks. Use the list_tasks tool with status='pending'."
        )
        await self._reply(update, response)

    @authorized_only
    @error_handler
    async def _cmd_addtask(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /addtask <description>."""
        text = update.message.text.replace("/addtask", "", 1).strip()
        if not text:
            await update.message.reply_text(
                "Usage: `/addtask <description>`\n"
                "Example: `/addtask Review PR #42 by Friday`",
                parse_mode="Markdown",
            )
            return

        response = await self.brain.chat(
            f"Create a new task for me: {text}. "
            "Use the add_task tool. Infer priority and due date if mentioned."
        )
        await self._reply(update, response)

    @authorized_only
    @error_handler
    async def _cmd_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /done <task_id>."""
        text = update.message.text.replace("/done", "", 1).strip()
        if not text or not text.isdigit():
            await update.message.reply_text(
                "Usage: `/done <task_id>`\nExample: `/done 3`",
                parse_mode="Markdown",
            )
            return

        response = await self.brain.chat(
            f"Mark task #{text} as completed. Use the complete_task tool."
        )
        await self._reply(update, response)

    @authorized_only
    @error_handler
    async def _cmd_notes(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /notes — show recent notes."""
        response = await self.brain.chat(
            "Show my recent notes. Use the get_recent_notes tool."
        )
        await self._reply(update, response)

    @authorized_only
    @error_handler
    async def _cmd_save(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /save <content>."""
        text = update.message.text.replace("/save", "", 1).strip()
        if not text:
            await update.message.reply_text(
                "Usage: `/save <content>`\n"
                "Example: `/save Great article about quantum computing at example.com`",
                parse_mode="Markdown",
            )
            return

        response = await self.brain.chat(
            f"Save this note for me: {text}. "
            "Use the save_note tool. Infer appropriate tags and category."
        )
        await self._reply(update, response)

    @authorized_only
    @error_handler
    async def _cmd_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /search <query>."""
        query = update.message.text.replace("/search", "", 1).strip()
        if not query:
            await update.message.reply_text(
                "Usage: `/search <query>`\nExample: `/search machine learning`",
                parse_mode="Markdown",
            )
            return

        response = await self.brain.chat(
            f"Search my notes for: {query}. Use the search_notes tool."
        )
        await self._reply(update, response)

    @authorized_only
    @error_handler
    async def _cmd_news(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /news — on-demand news digest."""
        await update.message.reply_text("📰 Fetching and curating latest news... This may take a moment.")

        raw_articles = await fetch_all_news()
        if "No news articles available" in raw_articles:
            await update.message.reply_text("😕 Couldn't fetch any news right now. Please try again later.")
            return

        digest = await self.brain.curate_news(raw_articles)

        # Save the digest
        today = datetime.now(Config.TIMEZONE).strftime("%Y-%m-%d")
        await self.db.save_digest(today, digest)

        await self._reply(update, digest)

    @authorized_only
    @error_handler
    async def _cmd_daily(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /daily — today's agenda."""
        response = await self.brain.chat(
            "Show me today's agenda. Use the get_today_agenda tool."
        )
        await self._reply(update, response)

    @authorized_only
    @error_handler
    async def _cmd_remind(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /remind <time> <message>."""
        text = update.message.text.replace("/remind", "", 1).strip()
        if not text:
            await update.message.reply_text(
                "Usage: `/remind <datetime> <message>`\n"
                "Example: `/remind 2025-12-31T09:00 New Year meeting`\n"
                "Or: `/remind tomorrow 9am Call the dentist`",
                parse_mode="Markdown",
            )
            return

        response = await self.brain.chat(
            f"Set a reminder for me: {text}. "
            "Use set_reminder with the appropriate ISO datetime. "
            "You already know the current time from the system prompt."
        )
        await self._reply(update, response)

    @authorized_only
    @error_handler
    async def _cmd_think(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /think <prompt> — deep reasoning mode."""
        text = update.message.text.replace("/think", "", 1).strip()
        if not text:
            await update.message.reply_text(
                "Usage: `/think <question or topic>`\n"
                "Example: `/think What are the pros and cons of microservices vs monolith?`",
                parse_mode="Markdown",
            )
            return

        await update.message.reply_text("🧠 Thinking deeply... This may take a moment.")
        response = await self.brain.think(text)
        await self._reply(update, response)

    @authorized_only
    @error_handler
    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /status — system status."""
        from storage.models import TaskStatus

        pending_tasks = await self.db.get_tasks(status=TaskStatus.PENDING)
        active_reminders = await self.db.get_active_reminders()
        recent_notes = await self.db.get_recent_notes(limit=1)

        now = datetime.now(Config.TIMEZONE)
        status = (
            f"ℹ️ **Second Brain Status**\n\n"
            f"🕐 Current time: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
            f"📌 Pending tasks: {len(pending_tasks)}\n"
            f"⏰ Active reminders: {len(active_reminders)}\n"
            f"📝 Notes stored: {'Yes' if recent_notes else 'None yet'}\n"
            f"🤖 Fast model: `{Config.FAST_MODEL}`\n"
            f"🧠 Deep model: `{Config.DEEP_MODEL}`\n"
            f"📰 News at: {Config.NEWS_DELIVERY_HOUR:02d}:{Config.NEWS_DELIVERY_MINUTE:02d}\n"
            f"📋 Agenda at: {Config.AGENDA_DELIVERY_HOUR:02d}:{Config.AGENDA_DELIVERY_MINUTE:02d}\n"
        )
        await update.message.reply_text(status, parse_mode="Markdown")

    # ─── Free-text Handler ────────────────────────────────────────────────

    @authorized_only
    @error_handler
    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle free-text messages — route to the AI agent."""
        text = update.message.text
        if not text:
            return

        # Show typing indicator
        await update.message.chat.send_action("typing")

        response = await self.brain.chat(text)
        await self._reply(update, response)

    # ─── Helpers ──────────────────────────────────────────────────────────

    async def _reply(self, update: Update, text: str) -> None:
        """Send a reply, handling long messages by splitting."""
        parts = split_long_message(text)
        for part in parts:
            try:
                await update.message.reply_text(part, parse_mode="Markdown")
            except Exception:
                # Fallback without Markdown formatting
                try:
                    await update.message.reply_text(part)
                except Exception:
                    logger.exception("Failed to reply to user.")

    @staticmethod
    async def _global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Global error handler for the Telegram application."""
        logger.error("Telegram error: %s", context.error, exc_info=context.error)
