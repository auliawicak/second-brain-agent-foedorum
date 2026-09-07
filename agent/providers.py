"""Provider adapters for the two OpenAI API styles used across the model pool.

- Responses API: OpenCode Zen's muse-spark-1.3-contributor-free.
- Chat Completions API: everything else (zen chat models, google, groq, etc).

A single lazy httpx client is shared by all providers to stay within the
process/connection budget.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx

from agent.registry import ModelSpec
from config import Config

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None


def shared_client() -> httpx.AsyncClient:
    """Return the process-wide http client, creating it lazily."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=Config.PROVIDER_TIMEOUT)
    return _client


async def shutdown_client() -> None:
    """Close the shared client on shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


class ProviderError(Exception):
    """A provider call failed. `status` is the HTTP status (or None)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def retryable(self) -> bool:
        if self.status is None:
            return True
        # 5xx / 429 are worth retrying; 4xx (except 429) are config problems.
        return self.status == 429 or self.status >= 500


@dataclass
class ProviderResult:
    text: str
    usage: dict[str, Any] | None = None
    tool_calls: list[dict[str, Any]] | None = None
    finish_reason: str | None = None


def _api_key_of(spec: ModelSpec) -> str:
    import os

    return os.environ.get(spec.api_key_env, "")


def _openai_headers(
    spec: ModelSpec, *, session_id: str | None = None, content_type: bool = False
) -> dict[str, str]:
    """Headers for an OpenAI-compatible provider call.

    OpenCode Zen/Go reject requests that omit `x-opencode-session`; the value
    just needs to be a stable per-conversation id (many proxies collapse the
    session for routing and prompt caching).
    """
    headers = {"Authorization": f"Bearer {_api_key_of(spec)}"}
    if content_type:
        headers["Content-Type"] = "application/json"
    session = session_id
    if not session and spec.provider == "zen":
        session = "secondbrain-proxy"
    if session:
        headers["x-opencode-session"] = session
    return headers


def _raise_for_response(resp: httpx.Response, spec: ModelSpec) -> None:
    if resp.status_code < 400:
        return
    detail = ""
    try:
        body = resp.json()
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            detail = err.get("message", "")[:300]
    except Exception:
        detail = resp.text[:300]
    raise ProviderError(
        f"{spec.provider}/{spec.id} HTTP {resp.status_code}: {detail}",
        status=resp.status_code,
    )


async def _call_responses(spec: ModelSpec, messages: list[dict], system_instruction: str,
                          temperature: float, max_tokens: int,
                          tools: list | None = None,
                          tool_choice: Any | None = None,
                          session_id: str | None = None) -> ProviderResult:
    """OpenAI-compatible `responses` endpoint (zen)."""
    payload: dict[str, Any] = {
        "model": spec.id,
        "input": _to_responses_messages(messages),
        "instructions": system_instruction or None,
        "temperature": temperature,
        "max_output_tokens": max_tokens,
    }
    if not payload.get("instructions"):
        payload.pop("instructions")
    converted_tools = _to_responses_tools(tools)
    if converted_tools:
        payload["tools"] = converted_tools
    payload["tool_choice"] = _to_responses_tool_choice(tool_choice)
    try:
        resp = await shared_client().post(
            f"{spec.base_url.rstrip('/')}/responses",
            headers=_openai_headers(spec, session_id=session_id),
            json=payload,
        )
    except httpx.HTTPError as e:
        raise ProviderError(f"{spec.provider}/{spec.id} request failed: {e}") from e
    _raise_for_response(resp, spec)
    data = resp.json()
    text, tool_calls, finish_reason = _extract_responses_items(data)
    return ProviderResult(
        text=text, usage=data.get("usage"),
        tool_calls=tool_calls, finish_reason=finish_reason,
    )


def _extract_responses_text(data: dict) -> str:
    text, _, _ = _extract_responses_items(data)
    return text


def _extract_responses_items(data: dict) -> tuple[str, list[dict] | None, str | None]:
    """Pull text + function-call tool_calls out of a Responses-API payload."""
    if not isinstance(data, dict):
        return "", None, None
    text = ""
    tool_calls: list[dict] = []
    for item in data.get("output", []):
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call":
            args = item.get("arguments")
            if isinstance(args, dict):
                args = json.dumps(args)
            tool_calls.append(
                {
                    "id": item.get("call_id") or item.get("id") or f"call_{uuid4().hex[:12]}",
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": args or "",
                    },
                }
            )
        if item.get("type") == "message":
            for c in item.get("content", []):
                if isinstance(c, dict) and c.get("type") == "output_text":
                    text += c.get("text", "")
        if item.get("type") == "output_text":
            text += item.get("text", "")
    if not tool_calls and not text:
        text = data.get("output_text") or ""
    finish_reason = "tool_calls" if tool_calls else ("stop" if text else None)
    return text, (tool_calls or None), finish_reason


def _to_responses_tools(tools: list | None) -> list | None:
    """Chat-completions tool shape → Responses-API tool shape.

    Hermes sends ``[{"type":"function","function":{name,description,parameters}}]``
    but OpenCode Zen's `/responses` schema wants the fields flattened on the
    tool object itself. Anything already Responses-shaped passes through.
    """
    if not tools:
        return None
    out: list[dict] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        if t.get("type") == "function" and "function" in t:
            fn = t.get("function") or {}
            item: dict[str, Any] = {"type": "function", "name": fn.get("name", "")}
            for key in ("description", "parameters", "strict"):
                if fn.get(key) is not None:
                    item[key] = fn[key]
            out.append(item)
        else:
            out.append(t)
    return out or None


def _to_responses_tool_choice(choice: Any) -> Any:
    if choice in ("auto", "none", "required") or choice is None:
        return choice
    if isinstance(choice, dict) and choice.get("type") == "function":
        fn = choice.get("function") or {}
        return {"type": "function", "name": fn.get("name", "")}
    return choice


def _to_responses_messages(messages: list[dict]) -> list[dict]:
    """Chat-completions message history → Responses-API input.

    Converts assistant `tool_calls` into the Responses `output` function_call
    items and tool results into `role: tool` messages with a `call_id`, so
    multi-turn tool loops survive the relay.
    """
    out: list[dict] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content")
        item: dict[str, Any] = {"role": role}
        if isinstance(content, str) and content:
            item["content"] = [{"type": "input_text", "text": content}]
        elif content is not None:
            item["content"] = content
        if role == "assistant" and m.get("tool_calls"):
            output = []
            for tc in m["tool_calls"]:
                if not isinstance(tc, dict) or tc.get("type") != "function":
                    continue
                fn = tc.get("function") or {}
                call_id = tc.get("id") or f"call_{uuid4().hex[:12]}"
                output.append(
                    {
                        "type": "function_call",
                        "id": call_id,
                        "call_id": call_id,
                        "name": fn.get("name", ""),
                        "arguments": fn.get("arguments", ""),
                    }
                )
            if output:
                item["output"] = output
        if role == "tool":
            item["call_id"] = m.get("tool_call_id") or f"call_{uuid4().hex[:12]}"
        out.append(item)
    return out


async def _call_chat(spec: ModelSpec, messages: list[dict], system_instruction: str,
                     temperature: float, max_tokens: int,
                     tools: list | None = None,
                     tool_choice: Any | None = None,
                     session_id: str | None = None) -> ProviderResult:
    """OpenAI-compatible `chat/completions` endpoint (everything else)."""
    chat_messages: list[dict] = []
    if system_instruction:
        chat_messages.append({"role": "system", "content": system_instruction})
    chat_messages.extend(messages)

    payload = {
        "model": spec.id,
        "messages": chat_messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice
    try:
        resp = await shared_client().post(
            f"{spec.base_url.rstrip('/')}/chat/completions",
            headers=_openai_headers(spec, session_id=session_id, content_type=True),
            json=payload,
        )
    except httpx.HTTPError as e:
        raise ProviderError(f"{spec.provider}/{spec.id} request failed: {e}") from e
    _raise_for_response(resp, spec)
    data = resp.json()
    text = ""
    tool_calls = None
    finish_reason = None
    if isinstance(data, dict):
        choices = data.get("choices") or []
        if choices:
            first = choices[0]
            msg = first.get("message") or {}
            text = msg.get("content") or ""
            if msg.get("tool_calls"):
                tool_calls = msg["tool_calls"]
            finish_reason = first.get("finish_reason")
    return ProviderResult(
        text=text, usage=data.get("usage") if isinstance(data, dict) else None,
        tool_calls=tool_calls, finish_reason=finish_reason,
    )


def _estimate_prompt_bytes(messages: list[dict], system_instruction: str | None) -> int:
    total = len(system_instruction or "")
    for m in messages:
        total += len(m.get("content", ""))
    return total


async def call_model(
    spec: ModelSpec,
    messages: list[dict],
    system_instruction: str,
    *,
    temperature: float = 0.7,
    max_tokens: int | None = None,
    tools: list | None = None,
    tool_choice: Any | None = None,
    session_id: str | None = None,
) -> ProviderResult:
    """Dispatch one call to a model through the right provider adapter."""
    if max_tokens is None:
        max_tokens = spec.max_output_tokens
    if spec.api_style == "responses":
        result = await _call_responses(
            spec, messages, system_instruction, temperature, max_tokens,
            tools=tools, tool_choice=tool_choice, session_id=session_id,
        )
    else:
        result = await _call_chat(
            spec, messages, system_instruction, temperature, max_tokens,
            tools=tools, tool_choice=tool_choice, session_id=session_id,
        )
    return result


def estimate_prompt_bytes(messages: list[dict], system_instruction: str) -> int:
    return _estimate_prompt_bytes(messages, system_instruction)