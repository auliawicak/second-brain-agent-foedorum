"""Tests for services.alerts — dedupe, truncation, and delivery."""

from __future__ import annotations

import asyncio

from services import alerts


class FakeResponse:
    def __init__(self, ok: bool = True):
        self.ok = ok

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError("boom")


class FakeClient:
    """Records payloads; returns a deterministic response."""

    def __init__(self):
        self.calls: list[dict] = []

    async def post(self, url: str, json: dict, **kwargs):
        self.calls.append(json)
        return FakeResponse(ok=True)


def _run(coro):
    return asyncio.run(coro)


def test_alert_sends_truncated_payload(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(alerts, "_get_client", lambda: fake)
    alerts._dedupe.clear()

    long_text = "A" * 5000
    ok = _run(alerts.alert_owner(long_text))
    assert ok
    assert len(fake.calls) == 1
    body = fake.calls[0]["text"]
    assert len(body) <= alerts._MAX_LEN + len("\n…(truncated)")
    assert body.endswith("…(truncated)")


def test_alert_dedupe_suppresses_duplicates_within_window(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(alerts, "_get_client", lambda: fake)
    alerts._dedupe.clear()

    ok1 = _run(alerts.alert_owner("hello", dedupe_key="k"))
    ok2 = _run(alerts.alert_owner("hello", dedupe_key="k"))
    assert ok1 is True
    assert ok2 is False
    assert len(fake.calls) == 1


def test_alert_no_dedupe_without_key(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(alerts, "_get_client", lambda: fake)
    alerts._dedupe.clear()

    _run(alerts.alert_owner("a"))
    _run(alerts.alert_owner("a"))
    assert len(fake.calls) == 2