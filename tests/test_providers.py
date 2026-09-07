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


class FakeResp:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class FakeClient:
    def __init__(self, body: dict):
        self.body = body
        self.sent: dict | None = None

    async def post(self, url, **kwargs):
        self.sent = {"url": url, **kwargs}
        return FakeResp(200, self.body)


@pytest.mark.asyncio
async def test_chat_adapter_forwards_tools_and_returns_tool_calls(monkeypatch) -> None:
    from agent.providers import _call_chat, ProviderResult
    from agent.registry import ModelSpec

    spec = ModelSpec(
        id="chat-tools", provider="google", base_url="http://x",
        api_style="chat_completions", api_key_env="X",
        tiers=frozenset({"chat"}), priority=0,
    )
    fake = FakeClient(
        {
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {"id": "c1", "type": "function",
                             "function": {"name": "get_weather", "arguments": "{}"}}
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
    )
    monkeypatch.setattr("agent.providers.shared_client", lambda: fake)

    result: ProviderResult = await _call_chat(
        spec, [{"role": "user", "content": "weather?"}], "you help",
        temperature=0.5, max_tokens=100,
        tools=[{"type": "function", "function": {"name": "get_weather", "parameters": {}}}],
        tool_choice="auto",
    )
    assert fake.sent is not None
    payload = fake.sent["json"]
    assert payload["tools"] is not None and payload["tool_choice"] == "auto"
    assert result.tool_calls == [
        {"id": "c1", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}
    ]
    assert result.finish_reason == "tool_calls"
    assert result.text == ""


@pytest.mark.asyncio
async def test_zen_calls_carry_x_opencode_session(monkeypatch) -> None:
    from agent.providers import _call_chat
    from agent.registry import ModelSpec

    spec = ModelSpec(
        id="muse-spark-1.3-contributor-free", provider="zen",
        base_url="https://opencode.ai/zen/v1", api_style="chat_completions",
        api_key_env="OPENCODE_ZEN_API_KEY", tiers=frozenset({"chat"}), priority=0,
    )
    fake = FakeClient({"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}]})
    monkeypatch.setattr("agent.providers.shared_client", lambda: fake)

    await _call_chat(spec, [{"role": "user", "content": "hi"}], "be terse",
                     temperature=0.2, max_tokens=8, session_id="sb-abc")
    assert fake.sent is not None
    headers = fake.sent["headers"]
    assert headers["x-opencode-session"] == "sb-abc"

    # A stable fallback keeps the free tier happy even if no session is passed.
    fake2 = FakeClient({"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}]})
    monkeypatch.setattr("agent.providers.shared_client", lambda: fake2)
    await _call_chat(spec, [{"role": "user", "content": "hi"}], "be terse",
                     temperature=0.2, max_tokens=8)
    assert fake2.sent is not None
    assert fake2.sent["headers"]["x-opencode-session"] == "secondbrain-proxy"


def test_responses_tool_conversion() -> None:
    from agent.providers import _to_responses_messages, _to_responses_tool_choice, _to_responses_tools

    tools = [
        {"type": "function", "function": {
            "name": "math_add", "description": "Add", "parameters": {"type": "object"},
        }}
    ]
    assert _to_responses_tools(tools) == [
        {"type": "function", "name": "math_add",
         "description": "Add", "parameters": {"type": "object"}}
    ]
    assert _to_responses_tools(None) is None
    assert _to_responses_tool_choice("auto") == "auto"
    assert _to_responses_tool_choice(
        {"type": "function", "function": {"name": "math_add"}}
    ) == {"type": "function", "name": "math_add"}

    converted = _to_responses_messages(
        [
            {"role": "user", "content": "compute"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "call_1", "type": "function",
                             "function": {"name": "math_add", "arguments": '{"a":2,"b":2}'}}]},
            {"role": "tool", "content": "4", "tool_call_id": "call_1"},
        ]
    )
    assert converted[0] == {"role": "user", "content": [{"type": "input_text", "text": "compute"}]}
    assert converted[1]["output"][0]["type"] == "function_call"
    assert converted[1]["output"][0]["call_id"] == "call_1"
    assert converted[1]["output"][0]["arguments"] == '{"a":2,"b":2}'
    assert converted[2] == {"role": "tool", "call_id": "call_1",
                            "content": [{"type": "input_text", "text": "4"}]}


@pytest.mark.asyncio
async def test_responses_adapter_returns_function_calls(monkeypatch) -> None:
    from agent.providers import _extract_responses_items

    data = {
        "output": [
            {"type": "function_call", "call_id": "fc1", "name": "list_tasks",
             "arguments": {"status": "pending"}},
            {"type": "message", "content": [{"type": "output_text", "text": "Checking…"}]},
        ]
    }
    text, calls, fr = _extract_responses_items(data)
    assert text == "Checking…"
    assert calls is not None and calls[0]["id"] == "fc1"
    assert calls[0]["function"] == {
        "name": "list_tasks", "arguments": '{"status": "pending"}'
    }
    assert fr == "tool_calls"