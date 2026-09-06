"""Provider adapter robustness tests (Phase 1)."""

import httpx
import pytest

from agent.providers import ProviderError, call_model
from agent.registry import get_registry


@pytest.mark.asyncio
async def test_network_error_becomes_retryable_provider_error(monkeypatch) -> None:
    spec = next(s for s in get_registry() if s.id == "muse-spark-1.3-contributor-free")

    async def boom(*args, **kwargs):
        raise httpx.ReadTimeout("timed out reading response")

    monkeypatch.setattr("agent.providers.shared_client", lambda: type(
        "FakeClient",
        (),
        {"post": boom},
    )())

    with pytest.raises(ProviderError) as excinfo:
        await call_model(spec, [{"role": "user", "content": "hi"}], "be terse")
    assert excinfo.value.status is None
    assert excinfo.value.retryable is True


@pytest.mark.asyncio
async def test_retryable_status_classification() -> None:
    assert ProviderError("x", status=429).retryable is True
    assert ProviderError("x", status=500).retryable is True
    assert ProviderError("x", status=503).retryable is True
    assert ProviderError("x", status=400).retryable is False
    assert ProviderError("x", status=401).retryable is False
    assert ProviderError("x", status=403).retryable is False
    assert ProviderError("x", status=None).retryable is True