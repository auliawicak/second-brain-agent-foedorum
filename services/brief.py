"""Morning brief (Phase 7 §7.1).

Merges the old separate 06:00 news job and 06:30 agenda job into ONE 06:00
message: today's tasks and reminders, overdue items that need a decision, and
a single relevant news item ("one thing worth reading", not ten). `/news`
still delivers the full digest on demand.

Deterministic day data (tasks/reminders) is rendered directly; only the
single-news pick uses the model, with a safe fallback to the first raw item.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from config import Config
from services.news import fetch_all_news

logger = logging.getLogger(__name__)

MAX_BRIEF_TASKS = 10
MAX_BRIEF_REMINDERS = 5
MAX_OVERDUE_IN_BRIEF = 3

_PRIORITY_ICONS = {"urgent": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}


async def build_morning_brief(db, brain) -> str:
    """Compose and persist the single 06:00 brief. Returns the message text."""
    now = datetime.now(Config.TIMEZONE)
    today = now.strftime("%Y-%m-%d")
    tasks = await db.get_today_tasks(today)

    today_lines: list = []
    overdue: list = []
    for t in tasks:
        if t.due_date and t.due_date < today:
            overdue.append(t)
        else:
            today_lines.append(t)

    lines = [f"☀️ **Good Morning — {today}**\n", "**📌 Your plan for today:**"]
    if today_lines:
        for t in today_lines[:MAX_BRIEF_TASKS]:
            icon = _PRIORITY_ICONS.get(t.priority.value, "")
            due = f" (due: {t.due_date})" if t.due_date else ""
            lines.append(f"  {icon} #{t.id} {t.description}{due}")
    else:
        lines.append("  All clear — nothing due today. 🎉")

    reminders = await db.get_active_reminders()
    if reminders:
        lines.append("\n**⏰ Reminders:**")
        for r in reminders[:MAX_BRIEF_REMINDERS]:
            lines.append(f"  • {r.message} — {r.trigger_time}")

    if overdue:
        lines.append(f"\n**⏳ Overdue — need a decision:**")
        for t in overdue[:MAX_OVERDUE_IN_BRIEF]:
            lines.append(
                f"  • #{t.id} {t.description} (due {t.due_date}) — reschedule, drop, or delegate?"
            )

    news_block = await _single_news_block(brain, tasks)
    if news_block:
        lines.append("\n**📰 One thing worth reading:**\n" + news_block)

    lines.append("\nHave a productive day! 💪")
    brief = "\n".join(lines)

    try:
        await db.save_digest(today, brief)
    except Exception:
        logger.exception("Failed to persist the morning brief.")

    return brief


async def _single_news_block(brain, tasks) -> str | None:
    """Return the formatted single-news block, or None when news is down."""
    try:
        raw = await fetch_all_news()
    except Exception:
        logger.exception("News fetch failed for the morning brief.")
        return None
    if not raw or "No news articles available" in raw:
        return None

    focus = " ".join(f"#{t.id} {t.description}" for t in (tasks or [])[:6]) or ""
    item = None
    try:
        item = await brain.curate_single_news_item(raw, context=focus)
    except Exception:
        logger.exception("Single-news pick failed; falling back to the first item.")
    if not item:
        item = _first_raw_article(raw)
    if not item:
        return None

    headline = (item.get("headline") or "").strip()
    if not headline:
        return None
    summary = (item.get("summary") or "").strip()
    why = (item.get("why") or "").strip()
    url = (item.get("url") or "").strip()

    block = f"**{headline}**"
    if summary:
        block += f"\n{summary}"
    if why:
        block += f"\n• *Why this matters:* {why}"
    if url:
        block += f"\n🔗 {url}"
    return block


def _first_raw_article(raw: str) -> dict:
    """Fallback: take the first 'Title:' line from the raw feed text."""
    title_m = re.search(r"^Title: (.+)$", raw, flags=re.MULTILINE)
    if not title_m:
        return {}
    url_m = re.search(r"^URL: (.+)$", raw, flags=re.MULTILINE)
    return {
        "headline": title_m.group(1).strip(),
        "summary": "",
        "why": "",
        "url": url_m.group(1).strip() if url_m else "",
    }


__all__ = ["build_morning_brief"]