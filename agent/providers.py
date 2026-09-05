"""Provider adapters for the two OpenAI API styles used across the model pool.

- Responses API: OpenCode Zen's muse-spark-1.3-contributor-free.
- Chat Completions API: everything else (zen chat models, google, groq, etc).

A single lazy httpx client is shared by all providers to stay within the
process/connection budget.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

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


def _api_key_of(spec: ModelSpec) -> str:
    import os

    return os.environ.get(spec.api_key_env, "")


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
                          temperature: float, max_tokens: int) -> ProviderResult:
    """OpenAI-compatible `responses` endpoint (zen)."""
    payload: dict[str, Any] = {
        "model": spec.id,
        "input": messages,
        "instructions": system_instruction or None,
        "temperature": temperature,
        "max_output_tokens": max_tokens,
    }
    if not payload.get("instructions"):
        payload.pop("instructions")
    resp = await shared_client().post(
        f"{spec.base_url.rstrip('/')}/responses",
        headers={"Authorization": f"Bearer {_api_key_of(spec)}"},
        json=payload,
    )
    _raise_for_response(resp, spec)
    data = resp.json()
    text = _extract_responses_text(data)
    return ProviderResult(text=text, usage=data.get("usage"))


def _extract_responses_text(data: dict) -> str:
    if not isinstance(data, dict):
        return ""
    for item in data.get("output", []):
        if isinstance(item, dict) and item.get("type") == "message":
            content = item.get("content", [])
            for c in content:
                if isinstance(c, dict) and c.get("type") == "output_text":
                    return c.get("text", "")
        if isinstance(item, dict) and item.get("type") == "output_text":
            return item.get("text", "")
    out = data.get("output_text")
    return out or ""


async def _call_chat(spec: ModelSpec, messages: list[dict], system_instruction: str,
                     temperature: float, max_tokens: int) -> ProviderResult:
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
    resp = await shared_client().post(
        f"{spec.base_url.rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {_api_key_of(spec)}",
            "Content-Type": "application/json",
        },
        json=payload,
    )
    _raise_for_response(resp, spec)
    data = resp.json()
    text = ""
    if isinstance(data, dict):
        choices = data.get("choices") or []
        if choices:
            msg = choices[0].get("message") or {}
            text = msg.get("content") or ""
    return ProviderResult(text=text, usage=data.get("usage") if isinstance(data, dict) else None)


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
) -> ProviderResult:
    """Dispatch one call to a model through the right provider adapter."""
    if max_tokens is None:
        max_tokens = spec.max_output_tokens
    if spec.api_style == "responses":
        result = await _call_responses(
            spec, messages, system_instruction, temperature, max_tokens
        )
    else:
        result = await _call_chat(
            spec, messages, system_instruction, temperature, max_tokens
        )
    return result


def estimate_prompt_bytes(messages: list[dict], system_instruction: str) -> int:
    return _estimate_prompt_bytes(messages, system_instruction)