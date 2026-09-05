"""Crash and downtime alerting to the owner over Telegram.

Uses a bare httpx POST to the Bot API so it works even when the
python-telegram-bot Application is broken or not yet built.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from config import Config

logger = logging.getLogger(__name__)

# Shared client — created lazily, reused across alerts.
_client: httpx.AsyncClient | None = None
_lock = asyncio.Lock()

_dedupe: dict[str, float] = {}
_DEDUPE_SECONDS = 30 * 60  # suppress identical alerts within 30 minutes
_MAX_LEN = 3000


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=15)
    return _client


async def alert_owner(text: str, *, dedupe_key: str | None = None) -> bool:
    """Send `text` to the owner's Telegram chat.

    Args:
        text: The message to send (truncated to 3000 chars).
        dedupe_key: If provided, suppress an identical-keyed alert that
            was sent within the last 30 minutes.

    Returns:
        True if the alert was delivered.
    """
    now = time.monotonic()

    if dedupe_key is not None:
        async with _lock:
            last = _dedupe.get(dedupe_key)
            if last is not None and now - last < _DEDUPE_SECONDS:
                logger.info("Suppressing duplicate alert: %r", dedupe_key)
                return False
            _dedupe[dedupe_key] = now

    body = text if len(text) <= _MAX_LEN else text[:_MAX_LEN] + "\n…(truncated)"

    try:
        resp = await _get_client().post(
            f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": Config.TELEGRAM_USER_ID,
                "text": body,
                "disable_notification": True,
            },
        )
        resp.raise_for_status()
        logger.info("Alert sent to owner (%d chars).", len(body))
        return True
    except Exception:
        logger.exception("Failed to send alert to owner")
        return False


async def shutdown() -> None:
    """Close the shared client (called on app shutdown)."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None