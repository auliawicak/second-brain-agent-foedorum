"""Telegram bot handler — commands, free-text routing, and application setup."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from datetime import datetime

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from agent.brain import ChatResult, SecondBrain
from bot.feedback import (
    decode_feedback_cb,
    detect_edit_request,
    encode_feedback_cb,
    parse_created_id,
    should_attach_feedback,
)
from bot.formatters import format_help_message, split_long_message
from bot.middleware import authorized_only, error_handler
from config import Config
from services.news import fetch_all_news
from storage.database import Database

logger = logging.getLogger(__name__)

EDIT_WINDOW_SECONDS = 600      # user edit/delete within 10 min of agent create
THUMBSDOWN_TIMEOUT = 300       # drop an unanswered 👎 prompt after 5 minutes
MSG_META_CAP = 500


class TelegramBot:
    """Telegram bot interface for the Second Brain.

    Registers all command handlers and routes free-text messages
    to the Antigravity agent.
    """

    def __init__(self, brain: SecondBrain, db: Database) -> None:
        self.brain = brain
        self.db = db
        self.app: Application | None = None
        # Phase 2 feedback state (all in-memory, small).
        self._pending_thumbsdown: dict[int, dict] = {}     # chat_id -> state
        self._msg_meta: dict[str, dict] = {}               # token -> metadata
        self._agent_created: list[dict] = []               # recent creates by agent

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

        # Inline feedback buttons (👍 / 👎) on non-trivial replies (Phase 2).
        self.app.add_handler(CallbackQueryHandler(self._handle_callback))

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
            "List all my pending tasks. Use the list_tasks tool with status='pending'.",
            confirmed=True,
        )
        await self._reply_chat(update, response)

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
            "Use the add_task tool. Infer priority and due date if mentioned.",
            confirmed=True,
        )
        await self._reply_chat(update, response)

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
            f"Mark task #{text} as completed. Use the complete_task tool.",
            confirmed=True,
        )
        await self._reply_chat(update, response)

    @authorized_only
    @error_handler
    async def _cmd_notes(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /notes — show recent notes."""
        response = await self.brain.chat(
            "Show my recent notes. Use the get_recent_notes tool.",
            confirmed=True,
        )
        await self._reply_chat(update, response)

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
            "Use the save_note tool. Infer appropriate tags and category.",
            confirmed=True,
        )
        await self._reply_chat(update, response)

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
            f"Search my notes for: {query}. Use the search_notes tool.",
            confirmed=True,
        )
        await self._reply_chat(update, response)

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
            "Show me today's agenda. Use the get_today_agenda tool.",
            confirmed=True,
        )
        await self._reply_chat(update, response)

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
            "You already know the current time from the system prompt.",
            confirmed=True,
        )
        await self._reply_chat(update, response)

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
        """Handle /status — system + model-pool status."""
        from agent.health import status_models
        from storage.models import TaskStatus

        pending_tasks = await self.db.get_tasks(status=TaskStatus.PENDING)
        active_reminders = await self.db.get_active_reminders()
        recent_notes = await self.db.get_recent_notes(limit=1)
        month_bytes = await self.brain.usage.month_prompt_bytes()
        corr7 = await self.db.get_correction_counts(days=7)
        up7, down7 = await self.db.get_feedback_counts(days=7)

        now = datetime.now(Config.TIMEZONE)
        status = (
            f"ℹ️ **Second Brain Status**\n\n"
            f"🕐 Current time: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
            f"📌 Pending tasks: {len(pending_tasks)}\n"
            f"⏰ Active reminders: {len(active_reminders)}\n"
            f"📝 Notes stored: {'Yes' if recent_notes else 'None yet'}\n"
            f"🌐 Egress MTD prompt: {month_bytes / 1024:.1f} KB\n"
            f"🗣️ Corrections (7d): {corr7}\n"
            f"👍/👎 Feedback (7d): {up7}/{down7}\n"
            f"📰 News at: {Config.NEWS_DELIVERY_HOUR:02d}:{Config.NEWS_DELIVERY_MINUTE:02d}\n"
            f"📋 Agenda at: {Config.AGENDA_DELIVERY_HOUR:02d}:{Config.AGENDA_DELIVERY_MINUTE:02d}\n\n"
        )

        # Per-model lines
        lines = []
        for m in await status_models(self.brain.health, self.brain.usage):
            state = "🟢" if m["state"] == "open" else "🔴"
            key = "🔑" if m["key_set"] else "🚫"
            budget = (
                f" {m['budget_pct']}%RPD" if m["budget_pct"] is not None else ""
            )
            rpm = " ⚠️RPM" if m["rpm_full"] else ""
            lines.append(
                f"`{m['id']}` {key} {state} "
                f"[{','.join(m['tiers'])}] P{m['priority']}"
                f"{budget}{rpm} · {m['today_calls']}c"
            )
        status += "**Model pool:**\n" + "\n".join(lines) if lines else "**Model pool:** empty"

        for part in split_long_message(status):
            await update.message.reply_text(part, parse_mode="Markdown")

    # ─── Free-text Handler ────────────────────────────────────────────────

    @authorized_only
    @error_handler
    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle free-text messages — route to the AI agent."""
        text = update.message.text
        if not text:
            return

        chat_id = update.effective_chat.id

        # Phase 2: if a 👎 follow-up prompt is waiting, this text is the
        # user's correction — capture it and acknowledge.
        if await self._capture_thumbsdown_reply(chat_id, text, update):
            return

        # Show typing indicator
        await update.message.chat.send_action("typing")

        prev = self.brain.last_chat  # previous agent turn, if any

        # Phase 2 §2.1: explicit correction classifier — only after a
        # tool-call turn. Fire-and-forget so the user's message isn't delayed.
        if prev and prev.executed_tool:
            self._prune_agent_created()
            asyncio.create_task(self._classify_correction_async(prev, text))

        response = await self.brain.chat(text)
        await self._reply_chat(update, response)

        # Phase 2 §2.1 'edit': user modifies/deletes something the agent
        # created within the last 10 minutes.
        await self._detect_edit(text)

    # ─── Phase 2: inline feedback buttons ─────────────────────────────────

    @authorized_only
    @error_handler
    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle 👍/👎 presses on feedback buttons."""
        cbq = update.callback_query
        parsed = decode_feedback_cb(cbq.data)
        msg = cbq.message
        if not parsed or not msg or not msg.chat:
            await cbq.answer("This button has expired.")
            return

        chat_id = msg.chat.id
        message_ref = f"{chat_id}:{msg.message_id}"
        meta = self._msg_meta.get(parsed["token"], {})

        await self.db.add_feedback(
            message_ref=message_ref,
            rating=parsed["rating"],
            model_id=meta.get("model_id"),
            tier=meta.get("tier"),
        )

        if parsed["rating"] == 1:
            await cbq.answer("Thanks! 👍")
            logger.info("Thumbs-up stored for %s", message_ref)
        else:
            self._pending_thumbsdown[chat_id] = {
                "message_ref": message_ref,
                "expires_at": time.time() + THUMBSDOWN_TIMEOUT,
            }
            await cbq.answer("🤔 What should I have done instead? Reply below.")
            logger.info("Thumbs-down stored for %s; awaiting correction", message_ref)

    # ─── Helpers ──────────────────────────────────────────────────────────

    async def _capture_thumbsdown_reply(self, chat_id: int, text: str, update: Update) -> bool:
        """Turn a 👎 follow-up reply into a stored correction. Returns True if consumed."""
        pending = self._pending_thumbsdown.get(chat_id)
        if not pending:
            return False
        if time.time() > pending["expires_at"]:
            self._pending_thumbsdown.pop(chat_id, None)
            return False
        del self._pending_thumbsdown[chat_id]
        meta = self._msg_meta.get(pending["message_ref"], {})
        await self.db.add_correction(
            trigger="thumbs_down",
            user_message=text,
            agent_action=meta.get("agent_action"),
            correction=text,
        )
        await update.message.reply_text("🙏 Got it — I'll keep that in mind next time.")
        logger.info("Correction captured (trigger=thumbs_down) for chat %s", chat_id)
        return True

    async def _classify_correction_async(self, prev: ChatResult, text: str) -> None:
        """Background explicit-correction classification after a tool turn."""
        try:
            result = await self.brain.classify_correction(text, prev.agent_action)
        except Exception:
            logger.exception("Correction classifier failed")
            return
        if result and result.get("is_correction"):
            await self.db.add_correction(
                trigger="explicit",
                user_message=text,
                agent_action=prev.agent_action,
                correction=result.get("what_was_wrong"),
            )
            logger.info(
                "Explicit correction stored after tool turn %s: %r",
                prev.tool, result.get("what_was_wrong"),
            )
        else:
            logger.info("Correction classifier: non-correction after tool turn %s", prev.tool)

    async def _detect_edit(self, text: str) -> None:
        """'Edit' detection: user changes/deletes an agent-created item."""
        target_id = detect_edit_request(text)
        if not target_id:
            return
        now = time.time()
        for entry in self._agent_created:
            if entry["id"] == target_id and now - entry["ts"] <= EDIT_WINDOW_SECONDS:
                await self.db.add_correction(
                    trigger="edit",
                    user_message=text,
                    agent_action=entry["agent_action"],
                    correction=(
                        f"task/note #{target_id} modified/deleted by user shortly "
                        f"after agent created it via {entry['kind']}"
                    ),
                )
                logger.info(
                    "Edit correction stored for #%s (%ss after %s)",
                    target_id, int(now - entry["ts"]), entry["kind"],
                )
                return

    def _feedback_markup(self, token: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("👍", callback_data=encode_feedback_cb(1, token)),
                    InlineKeyboardButton("👎", callback_data=encode_feedback_cb(-1, token)),
                ]
            ]
        )

    def _prune_msg_meta(self) -> None:
        if len(self._msg_meta) > MSG_META_CAP:
            for key in list(self._msg_meta)[: len(self._msg_meta) - MSG_META_CAP]:
                self._msg_meta.pop(key, None)

    def _prune_agent_created(self) -> None:
        cutoff = time.time() - EDIT_WINDOW_SECONDS
        self._agent_created = [
            e for e in self._agent_created if e["ts"] >= cutoff
        ][-50:]

    async def _reply_chat(self, update: Update, result: ChatResult) -> None:
        """Send a chat() result; attach 👍/👎 on non-trivial replies."""
        parts = split_long_message(result.text)
        attach = should_attach_feedback(result.text, result.executed_tool)
        token = secrets.token_urlsafe(6)
        if attach:
            self._msg_meta[token] = {
                "agent_action": result.agent_action,
                "model_id": result.model_id,
                "tier": result.tier,
            }
            self._prune_msg_meta()

        for i, part in enumerate(parts):
            markup = self._feedback_markup(token) if (attach and i == 0) else None
            try:
                await update.message.reply_text(
                    part, parse_mode="Markdown", reply_markup=markup
                )
            except Exception:
                # Fallback without Markdown formatting.
                try:
                    await update.message.reply_text(part, reply_markup=markup)
                except Exception:
                    logger.exception("Failed to reply to user.")

        # Track items the agent created so a quick follow-up edit is captured.
        if result.executed_tool and result.tool in ("add_task", "save_note"):
            created_id = parse_created_id(result.tool_result)
            if created_id:
                self._agent_created.append(
                    {
                        "ts": time.time(),
                        "id": created_id,
                        "kind": result.tool,
                        "agent_action": result.agent_action,
                    }
                )
                self._prune_agent_created()

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
