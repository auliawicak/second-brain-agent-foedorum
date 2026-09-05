"""Circuit breaker and usage tracking for the model pool (Phase 1).

Persists circuit state in `model_health` and daily call/usage counters in
`model_usage` (migration 3), so a restart never forgets an open breaker and
budgets survive reboots. In-memory RPM windows are the only volatile state.
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from datetime import datetime, timedelta

from agent.registry import ModelSpec, get_registry
from storage.database import Database

logger = logging.getLogger(__name__)

# Open the breaker after this many consecutive failures, then back off
# 15 min * 2 ** (failures - OPEN_THRESHOLD), capped at MAX_COOLDOWN.
OPEN_THRESHOLD = 3
BASE_COOLDOWN_MIN = 15
MAX_COOLDOWN_HOURS = 2
# Non-retryable errors (400/401/403) open the breaker harder.
HARD_BLOCK_HOURS = 6

# Errors we will never retry: they mean a config/key/model problem, not load.
NON_RETRYABLE_STATUSES = {400, 401, 403}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _now() -> datetime:
    return datetime.now()


class ModelHealth:
    """Circuit state per model, backed by the model_health table."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def _get(self, model_id: str) -> dict:
        cursor = await self.db.db.execute(
            "SELECT consecutive_failures, cooldown_until, last_error, last_success "
            "FROM model_health WHERE model_id = ?",
            (model_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else {}

    async def record_failure(self, model_id: str, error: str, retryable: bool) -> None:
        """Record a failed call and open/escalate the breaker as needed."""
        row = await self._get(model_id)
        failures = int(row.get("consecutive_failures", 0)) + 1

        cooldown_until: str | None = None
        if retryable:
            if failures >= OPEN_THRESHOLD:
                minutes = BASE_COOLDOWN_MIN * (2 ** (failures - OPEN_THRESHOLD))
                minutes = min(minutes, MAX_COOLDOWN_HOURS * 60)
                cooldown_until = (_now() + timedelta(minutes=minutes)).isoformat(
                    timespec="seconds"
                )
                logger.warning(
                    "Model %s tripped breaker (failure %d). Cooldown until %s.",
                    model_id, failures, cooldown_until,
                )
        else:
            cooldown_until = (_now() + timedelta(hours=HARD_BLOCK_HOURS)).isoformat(
                timespec="seconds"
            )
            logger.error(
                "Model %s hard-blocked (non-retryable error). Cooldown until %s.",
                model_id, cooldown_until,
            )

        await self.db.db.execute(
            """INSERT INTO model_health (model_id, consecutive_failures, cooldown_until, last_error, last_success)
               VALUES (?, ?, ?, ?, NULL)
               ON CONFLICT(model_id) DO UPDATE SET
                 consecutive_failures = excluded.consecutive_failures,
                 cooldown_until = excluded.cooldown_until,
                 last_error = excluded.last_error""",
            (model_id, failures, cooldown_until, (error or "")[:500]),
        )
        await self.db.db.commit()

    async def record_success(self, model_id: str) -> None:
        """Close the breaker after a successful call."""
        await self.db.db.execute(
            """INSERT INTO model_health (model_id, consecutive_failures, cooldown_until, last_error, last_success)
               VALUES (?, 0, NULL, NULL, ?)
               ON CONFLICT(model_id) DO UPDATE SET
                 consecutive_failures = 0,
                 cooldown_until = NULL,
                 last_error = NULL,
                 last_success = excluded.last_success""",
            (model_id, _now_iso()),
        )
        await self.db.db.commit()

    async def is_blocked(self, model_id: str) -> bool:
        """True while the breaker is open (cooldown still running)."""
        row = await self._get(model_id)
        cooldown = row.get("cooldown_until")
        if not cooldown:
            return False
        try:
            until = datetime.fromisoformat(cooldown)
        except (ValueError, TypeError):
            return False
        return _now() < until


class UsageTracker:
    """Daily budgets (model_usage) plus in-memory RPM windows."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._rpm_windows: dict[str, deque[datetime]] = defaultdict(deque)

    @staticmethod
    def day() -> str:
        return _now().strftime("%Y-%m-%d")

    @staticmethod
    def month_prefix() -> str:
        return _now().strftime("%Y-%m")

    def note_request(self, model_id: str) -> None:
        """Record a request at this instant for RPM accounting (memory only)."""
        self._rpm_windows[model_id].append(_now())
        self._prune(model_id)

    def _prune(self, model_id: str) -> None:
        window = self._rpm_windows[model_id]
        cutoff = _now() - timedelta(minutes=1)
        while window and window[0] < cutoff:
            window.popleft()

    def rpm_full(self, spec: ModelSpec) -> bool:
        """True if the model's per-minute budget is exhausted."""
        if not spec.rpm:
            return False
        self._prune(spec.id)
        return len(self._rpm_windows[spec.id]) >= spec.rpm

    async def today_calls(self, model_id: str) -> int:
        cursor = await self.db.db.execute(
            "SELECT calls FROM model_usage WHERE model_id = ? AND day = ?",
            (model_id, self.day()),
        )
        row = await cursor.fetchone()
        return int(row["calls"]) if row else 0

    async def at_rpd_budget(self, spec: ModelSpec) -> bool:
        """True if the model has already used >= 80% of its daily allowance."""
        if not spec.rpd:
            return False
        calls = await self.today_calls(spec.id)
        return calls >= int(0.8 * spec.rpd)

    async def record_call(
        self, model_id: str, prompt_bytes: int, output_tokens_est: int, errored: bool = False
    ) -> None:
        """Upsert one call into model_usage."""
        calls_delta = 1
        errors_delta = 1 if errored else 0
        await self.db.db.execute(
            """INSERT INTO model_usage
                 (model_id, day, calls, errors, prompt_bytes, output_tokens_est)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(model_id, day) DO UPDATE SET
                 calls = calls + excluded.calls,
                 errors = errors + excluded.errors,
                 prompt_bytes = prompt_bytes + excluded.prompt_bytes,
                 output_tokens_est = output_tokens_est + excluded.output_tokens_est""",
            (
                model_id, self.day(), calls_delta, errors_delta,
                int(prompt_bytes), int(output_tokens_est),
            ),
        )
        await self.db.db.commit()

    async def month_prompt_bytes(self) -> int:
        """Total prompt bytes this calendar month (for the /status egress meter)."""
        prefix = self.month_prefix()
        cursor = await self.db.db.execute(
            "SELECT COALESCE(SUM(prompt_bytes), 0) FROM model_usage WHERE day LIKE ?",
            (f"{prefix}-%",),
        )
        row = await cursor.fetchone()
        return int(row[0])


def _tier_summary(spec: ModelSpec) -> list[str]:
    return sorted(t for t in spec.tiers if t != "vision" and t != "audio")


async def status_models(health: "ModelHealth", usage: "UsageTracker") -> list[dict]:
    """Per-model report used by /status: circuit state, calls, MTD bytes."""
    now = _now()
    out: list[dict] = []
    for spec in get_registry():
        row = await health._get(spec.id)
        cooldown = row.get("cooldown_until")
        blocked = False
        if cooldown:
            try:
                blocked = datetime.fromisoformat(cooldown) > now
            except (ValueError, TypeError):
                blocked = False
        calls = await usage.today_calls(spec.id)
        out.append(
            {
                "id": spec.id,
                "provider": spec.provider,
                "tiers": _tier_summary(spec),
                "priority": spec.priority,
                "state": "blocked" if blocked else "open",
                "cooldown_until": cooldown,
                "consecutive_failures": int(row.get("consecutive_failures", 0)) or 0,
                "last_error": row.get("last_error"),
                "last_success": row.get("last_success"),
                "today_calls": calls,
                "budget_pct": round(100.0 * calls / spec.rpd, 1) if spec.rpd else None,
                "rpd": spec.rpd,
                "rpm_full": usage.rpm_full(spec),
                "key_set": bool(__import__("os").environ.get(spec.api_key_env, "")),
            }
        )
    return out