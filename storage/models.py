"""Pydantic models for all data entities in the Second Brain."""

from __future__ import annotations

import enum
from datetime import datetime

from pydantic import BaseModel, Field


# ─── Enums ────────────────────────────────────────────────────────────────────


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    ARCHIVED = "archived"


class TaskPriority(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


# ─── Task ─────────────────────────────────────────────────────────────────────


class Task(BaseModel):
    """A work or daily task."""

    id: int | None = None
    description: str
    status: TaskStatus = TaskStatus.PENDING
    priority: TaskPriority = TaskPriority.MEDIUM
    due_date: str | None = None  # ISO date string (YYYY-MM-DD)
    category: str = "general"  # e.g. "work", "personal", "health"
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    completed_at: str | None = None
    recurring_cron: str | None = None  # Cron expression for recurring tasks


class TaskCreate(BaseModel):
    """Schema for creating a new task (used by agent structured output)."""

    description: str
    priority: TaskPriority = TaskPriority.MEDIUM
    due_date: str | None = None
    category: str = "general"
    recurring_cron: str | None = None


# ─── Note ─────────────────────────────────────────────────────────────────────


class Note(BaseModel):
    """A saved note, idea, or piece of information."""

    id: int | None = None
    content: str
    tags: list[str] = Field(default_factory=list)
    category: str = "general"
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())


class NoteCreate(BaseModel):
    """Schema for creating a new note."""

    content: str
    tags: list[str] = Field(default_factory=list)
    category: str = "general"


# ─── Reminder ─────────────────────────────────────────────────────────────────


class Reminder(BaseModel):
    """A scheduled reminder."""

    id: int | None = None
    message: str
    trigger_time: str  # ISO datetime string
    is_recurring: bool = False
    cron_expression: str | None = None
    is_active: bool = True
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())


class ReminderCreate(BaseModel):
    """Schema for creating a new reminder."""

    message: str
    trigger_time: str  # ISO datetime string or natural language (parsed by agent)
    is_recurring: bool = False
    cron_expression: str | None = None


# ─── Conversation Log ────────────────────────────────────────────────────────


class ConversationEntry(BaseModel):
    """A single message in the conversation history (for memory search)."""

    id: int | None = None
    role: str  # "user" or "assistant"
    content: str
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())


# ─── Daily Digest ─────────────────────────────────────────────────────────────


class NewsArticle(BaseModel):
    """A single curated news article."""

    headline: str
    summary: str
    category: str
    source: str
    url: str
    insight: str = ""  # "Why this matters"


class DailyDigest(BaseModel):
    """The morning news digest."""

    id: int | None = None
    date: str  # ISO date (YYYY-MM-DD)
    articles: list[NewsArticle] = Field(default_factory=list)
    raw_content: str = ""  # Full formatted digest text
    delivered_at: str | None = None


# ─── User Preferences / Habits ────────────────────────────────────────────────


class Preference(BaseModel):
    """A learned user preference or habit.

    Phase 6: gained `fact` (the statement), `category`, `keywords`,
    `confidence`, `evidence_count`, `first_seen/last_seen`, `is_core`,
    `superseded_by` (supersession chain, never overwrite) and `source_refs`.
    `key`/`value` remain for backward compatibility with the original tool.
    """

    id: int | None = None
    key: str  # A short identifier or topic (e.g., 'morning_drink', 'workout_time')
    value: str  # The actual preference or habit details
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    fact: str = ""
    category: str = "personal"
    keywords: str = ""
    confidence: float = 0.5
    evidence_count: int = 1
    first_seen: str | None = None
    last_seen: str | None = None
    is_core: int = 0
    superseded_by: int | None = None
    source_refs: str | None = None


class PreferenceCreate(BaseModel):
    """Schema for creating a new user preference."""

    key: str
    value: str
