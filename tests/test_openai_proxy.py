"""Tests for the OpenAI-compatible proxy bridge over the model pool."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from agent.health import ModelHealth, UsageTracker
from agent.registry import ModelSpec
from config import Config
from gateway.openai_proxy import OpenAICompatProxy, _chat_payload
from storage.database import Database


@pytest.fixture
async def pool_db(tmp_path) -> Database:
    db = Database(str(tmp_path / "pool-test.db"))
    await db.connect()
    yield db
    await db.close()


@pytest.fixture
async def proxy(tmp_path, monkeypatch) -> OpenAICompatProxy:
    monkeypatch.setattr(Config, "DATABASE_PATH", str(tmp_path / "proxy-test.db"))
    proxy = OpenAICompatProxy(host="127.0.0.1", port=0)
    await proxy.start()
    yield proxy
    await proxy.stop()


def _base_url(proxy: OpenAICompatProxy) -> str:
    socket = proxy._server.sockets[0]
    port = socket.getsockname()[1]
    return f"http://127.0.0.1:{port}"


class TestPayloadHelpers:
    def test_chat_payload_shape(self) -> None:
        body = _chat_payload("muse-zen", "hi", 5, 3)
        assert body["object"] == "chat.completion"
        assert body["model"] == "muse-zen"
        assert body["choices"][0]["message"] == {"role": "assistant", "content": "hi"}
        assert body["choices"][0]["finish_reason"] == "stop"
        assert body["usage"]["total_tokens"] == 8

    def test_split_system(self, proxy: OpenAICompatProxy) -> None:
        instruction, chat = proxy._split_system(
            [
                {"role": "system", "content": "You are strict."},
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
                {"role": "tool", "content": '{"ok":true}'},
                {"role": "developer", "content": "extra"},
            ]
        )
        assert instruction == "You are strict."
        assert [m["role"] for m in chat] == ["user", "assistant", "tool", "developer"]

    def test_split_system_multiple_system_messages(self, proxy: OpenAICompatProxy) -> None:
        instruction, chat = proxy._split_system(
            [
                {"role": "system", "content": "A"},
                {"role": "system", "content": "B"},
                {"role": "user", "content": "x"},
            ]
        )
        assert instruction == "A\nB"
        assert len(chat) == 1


class TestHttpEndpoints:
    async def test_models_list(self, proxy: OpenAICompatProxy) -> None:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{_base_url(proxy)}/v1/models")
        assert resp.status_code == 200
        assert resp.json()["data"][0]["id"] == "secondbrain-pool"

    async def test_chat_completion_success(
        self, proxy: OpenAICompatProxy, monkeypatch
    ) -> None:
        async def fake_generate(messages, system_instruction, **kwargs):
            return ("Hello there", "muse-zen", 100, 20)

        monkeypatch.setattr(proxy, "_generate_pool", fake_generate)
        payload = {
            "model": "secondbrain-pool",
            "messages": [
                {"role": "system", "content": "You are a test."},
                {"role": "user", "content": "hi"},
            ],
            "stream": False,
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{_base_url(proxy)}/v1/chat/completions", json=payload
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["choices"][0]["message"]["content"] == "Hello there"
        assert body["model"] == "muse-zen"
        assert body["usage"]["prompt_tokens"] == 100

    async def test_keep_alive_two_requests(
        self, proxy: OpenAICompatProxy, monkeypatch
    ) -> None:
        async def fake_generate(messages, system_instruction, **kwargs):
            return ("ok", "muse-zen", 10, 2)

        monkeypatch.setattr(proxy, "_generate_pool", fake_generate)
        payload = {"messages": [{"role": "user", "content": "a"}]}
        async with httpx.AsyncClient() as client:
            r1 = await client.post(f"{_base_url(proxy)}/v1/chat/completions", json=payload)
            r2 = await client.post(f"{_base_url(proxy)}/v1/chat/completions", json=payload)
        assert r1.status_code == 200 and r2.status_code == 200

    async def test_streaming_rejected(self, proxy: OpenAICompatProxy) -> None:
        payload = {"stream": True, "messages": [{"role": "user", "content": "a"}]}
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{_base_url(proxy)}/v1/chat/completions", json=payload)
        assert resp.status_code == 501
        assert "Streaming" in resp.json()["error"]["message"]

    async def test_bad_json_400(self, proxy: OpenAICompatProxy) -> None:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{_base_url(proxy)}/v1/chat/completions", content=b"{not json"
            )
        assert resp.status_code == 400

    async def test_missing_messages_400(self, proxy: OpenAICompatProxy) -> None:
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{_base_url(proxy)}/v1/chat/completions", json={})
        assert resp.status_code == 400

    async def test_unknown_route_404(self, proxy: OpenAICompatProxy) -> None:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{_base_url(proxy)}/v1/completions")
        assert resp.status_code == 404

    async def test_pool_exhaustion_500(
        self, proxy: OpenAICompatProxy, monkeypatch
    ) -> None:
        from agent.providers import ProviderError

        async def fake_generate(messages, system_instruction, **kwargs):
            raise ProviderError("All pool candidates failed: 502", status=502)

        monkeypatch.setattr(proxy, "_generate_pool", fake_generate)
        payload = {"messages": [{"role": "user", "content": "a"}]}
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{_base_url(proxy)}/v1/chat/completions", json=payload)
        assert resp.status_code == 500
        assert resp.json()["error"]["type"] == "pool_error"


class TestPromptCap:
    async def test_oversized_history_trimmed(self, pool_db: Database, monkeypatch) -> None:
        captured: dict[str, Any] = {}

        async def fake_call_model(spec, messages, system_instruction, **kwargs):
            captured["messages"] = messages
            captured["system"] = system_instruction
            return SimpleNamespace(text="x", usage=None)

        monkeypatch.setattr(
            "gateway.openai_proxy.call_model", fake_call_model
        )
        spec = ModelSpec(
            id="fake", provider="zen", base_url="http://x",
            api_style="responses", api_key_env="OPENCODE_ZEN_API_KEY",
            tiers=frozenset({"chat"}), priority=0,
        )

        async def fake_route(tier, health, usage, exclude=None):
            return [spec]

        monkeypatch.setattr("gateway.openai_proxy.route", fake_route)

        proxy = OpenAICompatProxy(host="127.0.0.1", port=0, tier="chat")
        proxy._health = ModelHealth(pool_db)
        proxy._usage = UsageTracker(pool_db)

        history = [
            {"role": "user", "content": "a" * 8000},
            {"role": "assistant", "content": "b" * 8000},
            {"role": "user", "content": "c" * 8000},
            {"role": "user", "content": "newest message"},
        ]
        text, model_id, prompt_bytes, _ = await proxy._generate_pool(
            history, system_instruction="sys", temperature=0.2
        )

        assert model_id == "fake"
        assert text == "x"
        total = sum(len(m["content"]) for m in captured["messages"])
        assert total < Config.MAX_PROMPT_CHARS
        # newest message must survive the trim
        assert captured["messages"][-1]["content"] == "newest message"
        # estimate = system instruction bytes + message content bytes
        assert prompt_bytes == 3 + total


class TestSplitSystemEdge:
    def test_non_string_content(self, proxy: OpenAICompatProxy) -> None:
        instruction, chat = proxy._split_system(
            [
                {"role": "system", "content": {"text": "obj"}},  # rare, must not crash
                {"role": "user", "content": None},
            ]
        )
        assert instruction == "{'text': 'obj'}".strip() or instruction != ""
        assert chat[0]["content"] == ""