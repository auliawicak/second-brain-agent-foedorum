"""Unit tests for agent.health — circuit breaker, usage tracking, RPM windows."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from agent.health import ModelHealth, UsageTracker, OPEN_THRESHOLD
from storage.database import Database


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "test.db"))
    await db.connect()
    yield db
    await db.close()


@pytest.fixture
async def health(db: Database) -> ModelHealth:
    return ModelHealth(db)


@pytest.fixture
async def usage(db: Database) -> UsageTracker:
    return UsageTracker(db)


class TestModelHealth:
    @pytest.mark.asyncio
    async def test_success_resets_failures(self, health: ModelHealth) -> None:
        await health.record_failure("test-model", "err", retryable=True)
        row = await health._get("test-model")
        assert int(row["consecutive_failures"]) == 1
        await health.record_success("test-model")
        row = await health._get("test-model")
        assert int(row["consecutive_failures"]) == 0
        assert row["cooldown_until"] is None

    @pytest.mark.asyncio
    async def test_breaker_opens_at_threshold(self, health: ModelHealth) -> None:
        for _ in range(OPEN_THRESHOLD):
            await health.record_failure("m1", "timeout", retryable=True)
        assert await health.is_blocked("m1") is True

    @pytest.mark.asyncio
    async def test_non_retryable_sets_6h_cooldown(self, health: ModelHealth) -> None:
        await health.record_failure("m2", "401 Unauthorized", retryable=False)
        assert await health.is_blocked("m2") is True
        # cooldown is ~6 hours from now
        row = await health._get("m2")
        until = datetime.fromisoformat(row["cooldown_until"])
        delta = until - datetime.now()
        assert 5 * 3600 < delta.total_seconds() < 7 * 3600


class TestUsageTracker:
    def test_rpm_full(self, usage: UsageTracker) -> None:
        from agent.registry import ModelSpec
        spec = ModelSpec(
            id="rpm-test", provider="zen", base_url="http://x",
            api_style="responses", api_key_env="X",
            tiers=frozenset({"chat"}), priority=0, rpm=2,
        )
        assert usage.rpm_full(spec) is False
        usage.note_request("rpm-test")
        usage.note_request("rpm-test")
        assert usage.rpm_full(spec) is True

    @pytest.mark.asyncio
    async def test_today_calls(self, usage: UsageTracker) -> None:
        assert await usage.today_calls("never-called") == 0
        await usage.record_call("test-model", prompt_bytes=500, output_tokens_est=100)
        assert await usage.today_calls("test-model") == 1
        await usage.record_call("test-model", prompt_bytes=500, output_tokens_est=100)
        assert await usage.today_calls("test-model") == 2

    @pytest.mark.asyncio
    async def test_at_rpd_budget(self, usage: UsageTracker) -> None:
        from agent.registry import ModelSpec
        spec = ModelSpec(
            id="budget-test", provider="zen", base_url="http://x",
            api_style="responses", api_key_env="X",
            tiers=frozenset({"chat"}), priority=0, rpd=10,
        )
        assert await usage.at_rpd_budget(spec) is False
        for _ in range(8):
            await usage.record_call("budget-test", 100, 10)
        assert await usage.at_rpd_budget(spec) is True
