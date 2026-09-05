"""Security middleware for the Telegram bot.

Restricts access to the authorized user only and provides error handling.
"""

from __future__ import annotations

import logging
from functools import wraps
from typing import Any, Callable, Coroutine

from telegram import Update
from telegram.ext import ContextTypes

from config import Config

logger = logging.getLogger(__name__)


def authorized_only(
    func: Callable[..., Coroutine[Any, Any, None]],
) -> Callable[..., Coroutine[Any, Any, None]]:
    """Decorator that restricts a handler to the configured TELEGRAM_USER_ID.

    Unauthorized users receive a polite rejection message.
    """

    @wraps(func)
    async def wrapper(self, update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not update.effective_user:
            return

        user_id = update.effective_user.id

        if user_id != Config.TELEGRAM_USER_ID:
            logger.warning(
                "Unauthorized access attempt from user %d (%s)",
                user_id,
                update.effective_user.username or "unknown",
            )
            if update.message:
                await update.message.reply_text(
                    "🚫 Sorry, I'm a personal assistant and only respond to my owner. "
                    "If you'd like your own Second Brain, check out the project on GitHub!"
                )
            return

        return await func(self, update, context, *args, **kwargs)

    return wrapper


def error_handler(
    func: Callable[..., Coroutine[Any, Any, None]],
) -> Callable[..., Coroutine[Any, Any, None]]:
    """Decorator that catches exceptions in handlers and sends a user-friendly error."""

    @wraps(func)
    async def wrapper(self, update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        try:
            return await func(self, update, context, *args, **kwargs)
        except Exception as e:
            logger.exception("Error in handler '%s': %s", func.__name__, e)
            if update and update.message:
                await update.message.reply_text(
                    "⚠️ Oops, something went wrong processing your request. "
                    "I've logged the error and will look into it. Please try again."
                )

    return wrapper
