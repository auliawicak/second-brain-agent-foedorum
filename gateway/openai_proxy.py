"""OpenAI-compatible HTTP bridge that exposes the Second Brain model pool.

Hermes Agent (and any other OpenAI-compatible client) can point a "Custom
Endpoint" provider at this server. Internally every request goes through the
same tiered pool as the Telegram bot — routing, health circuit-breakers,
usage tracking, and the hard `MAX_PROMPT_CHARS` cap all keep working, so the
free-tier economics survive the front-end migration.

Only `POST /v1/chat/completions` and `GET /v1/models` are implemented (the
Hermes OpenAI contract). `stream=false` only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any
from uuid import uuid4

from agent.context import build_context
from agent.health import ModelHealth, UsageTracker
from agent.providers import (
    ProviderError,
    call_model,
    estimate_prompt_bytes,
)
from agent.router import route
from config import Config
from storage.database import Database

logger = logging.getLogger(__name__)

DEFAULT_HOST = os.getenv("MODEL_PROXY_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.getenv("MODEL_PROXY_PORT", "18080"))
MODEL_NAME = os.getenv("MODEL_PROXY_MODEL", "secondbrain-pool")
MAX_CONCURRENT = int(os.getenv("MODEL_PROXY_MAX_CONCURRENT", "3"))


def _chat_payload(
    model: str,
    text: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


class OpenAICompatProxy:
    """A tiny asyncio HTTP/1.1 server in front of the model pool."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        tier: str = "chat",
    ) -> None:
        self.host = host
        self.port = port
        self.tier = tier
        self._server: asyncio.Server | None = None
        self._db: Database | None = None
        self._health: ModelHealth | None = None
        self._usage: UsageTracker | None = None
        self._sem = asyncio.Semaphore(MAX_CONCURRENT)

    async def start(self) -> None:
        self._db = Database(Config.DATABASE_PATH)
        await self._db.connect()
        self._health = ModelHealth(self._db)
        self._usage = UsageTracker(self._db)
        self._server = await asyncio.start_server(
            self._handle_connection,
            self.host,
            self.port,
        )
        logger.info("Model pool proxy listening on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self._db:
            await self._db.close()

    # ── request handling ──────────────────────────────────────────────────

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            while True:
                request_line = await reader.readline()
                if not request_line:
                    break
                line = request_line.decode("utf-8", "replace").strip()
                if not line:
                    break
                parts = line.split()
                if len(parts) < 2:
                    break
                method, path = parts[0], parts[1]

                headers: dict[str, str] = {}
                while True:
                    header_line = await reader.readline()
                    if not header_line or header_line in (b"\r\n", b"\n"):
                        break
                    decoded = header_line.decode("utf-8", "replace").strip()
                    if ":" in decoded:
                        key, value = decoded.split(":", 1)
                        headers[key.strip().lower()] = value.strip()

                content_length = int(headers.get("content-length", "0"))
                body = await reader.readexactly(content_length) if content_length else b""

                status, json_body = await self._dispatch(method, path, body)
                response = self._build_response(status, json_body, headers)

                close = headers.get("connection", "").lower() == "close" or parts[2] == "HTTP/1.0"
                if close:
                    response = response.replace(b"Connection: keep-alive", b"Connection: close")
                    writer.write(response)
                    await writer.drain()
                    break
                writer.write(response)
                await writer.drain()
        except asyncio.IncompleteReadError:
            pass
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception:  # noqa: BLE001 - a malformed client must not kill the proxy
            logger.exception("proxy request handler error")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError, OSError):
                pass

    def _build_response(self, status: int, json_body: dict | list, headers: dict) -> bytes:
        payload = json.dumps(json_body).encode("utf-8")
        status_line = {
            200: b"HTTP/1.1 200 OK",
            400: b"HTTP/1.1 400 Bad Request",
            404: b"HTTP/1.1 404 Not Found",
            429: b"HTTP/1.1 429 Too Many Requests",
            500: b"HTTP/1.1 500 Internal Server Error",
            501: b"HTTP/1.1 501 Not Implemented",
        }.get(status, b"HTTP/1.1 500 Internal Server Error")
        lines = [
            status_line,
            b"Content-Type: application/json",
            b"Content-Length: " + str(len(payload)).encode(),
            b"Connection: keep-alive",
            b"",
            b"",
        ]
        return b"\r\n".join(lines) + payload

    async def _dispatch(
        self,
        method: str,
        path: str,
        body: bytes,
    ) -> tuple[int, dict | list]:
        if method == "GET" and path == "/v1/models":
            return 200, {
                "object": "list",
                "data": [
                    {
                        "id": MODEL_NAME,
                        "object": "model",
                        "created": int(time.time()),
                        "owned_by": "second-brain",
                    }
                ],
            }

        if method == "POST" and path.rstrip("/") in (
            "/v1/chat/completions",
            "/chat/completions",
        ):
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                return 400, {"error": {"message": "Invalid JSON body", "type": "invalid_request"}}
            if payload.get("stream"):
                return 501, {
                    "error": {
                        "message": "Streaming is not supported; use stream=false",
                        "type": "invalid_request",
                    }
                }
            return await self._chat_completion(payload)

        return 404, {"error": {"message": f"No route {method} {path}", "type": "not_found"}}

    async def _chat_completion(self, payload: dict) -> tuple[int, dict]:
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            return 400, {"error": {"message": "messages is required", "type": "invalid_request"}}

        system_instruction, chat_messages = self._split_system(messages)
        max_tokens = payload.get("max_tokens") or payload.get("max_completion_tokens")
        temperature = payload.get("temperature", 0.7)

        try:
            async with self._sem:
                text, model_id, prompt_tokens, completion_tokens = await self._generate_pool(
                    chat_messages,
                    system_instruction,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
        except ProviderError as e:
            status = 429 if e.status == 429 else 500
            return status, {"error": {"message": str(e)[:300], "type": "pool_error"}}
        except Exception as e:  # noqa: BLE001
            logger.exception("pool generation failed")
            return 500, {"error": {"message": str(e)[:300], "type": "pool_error"}}

        if not text:
            text = ""  # let the client treat it as an empty assistant turn
        return 200, _chat_payload(
            model_id or payload.get("model") or MODEL_NAME,
            text,
            prompt_tokens,
            completion_tokens,
        )

    def _split_system(self, messages: list[dict]) -> tuple[str, list[dict]]:
        system_parts: list[str] = []
        chat: list[dict] = []

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content")
            if content is None:
                content = ""
            if role == "system":
                system_parts.append(str(content))
            elif role in ("user", "assistant", "tool", "developer"):
                chat.append({"role": role, "content": str(content)})
        system_instruction = "\n".join(
            p for p in system_parts if p
        ).strip()[: Config.MAX_PROMPT_CHARS]
        return system_instruction, chat

    async def _generate_pool(
        self,
        messages: list[dict],
        system_instruction: str,
        *,
        max_tokens: int | None = None,
        temperature: float = 0.7,
    ) -> tuple[str, str, int, int]:
        """One pooled call with failover, retries, and breaker/usage tracking.

        Mirrors `SecondBrain._generate` so the free-tier economics stay intact.
        """
        assert self._health is not None and self._usage is not None
        messages = build_context(messages, system_instruction)
        prompt_bytes = estimate_prompt_bytes(messages, system_instruction)
        exclude: set[str] = set()
        last_error: Exception | None = None

        candidates = await route(self.tier, self._health, self._usage, exclude)
        for cand in candidates:
            self._usage.note_request(cand.id)
            for attempt in range(2):
                try:
                    result = await call_model(
                        cand,
                        messages,
                        system_instruction,
                        temperature=temperature,
                        max_tokens=max_tokens,
                    )
                    est_out = max(1, len(result.text) // 4)
                    await self._usage.record_call(cand.id, prompt_bytes, est_out)
                    await self._health.record_success(cand.id)
                    return result.text, cand.id, prompt_bytes, est_out
                except ProviderError as e:
                    last_error = e
                    await self._usage.record_call(cand.id, prompt_bytes, 0, errored=True)
                    await self._health.record_failure(
                        cand.id, str(e), retryable=e.retryable
                    )
                    retryable = e.retryable and attempt + 1 < 2
                    if retryable:
                        await asyncio.sleep(3 * (attempt + 1))
                        continue
                    exclude.add(cand.id)
                    break

        raise ProviderError(f"All pool candidates failed: {last_error}")

    async def smoke_check(self) -> str:
        """One tiny call to prove the pool works before the gateway attaches."""
        text, model_id, _, _ = await self._generate_pool(
            [{"role": "user", "content": "Reply with exactly: ok"}],
            "You are a smoke test.",
        )
        return f"{model_id}: {text}"


async def _serve_forever(proxy: OpenAICompatProxy) -> None:
    await proxy.start()
    try:
        await asyncio.Event().wait()
    finally:
        await proxy.stop()


def main() -> None:
    """Standalone entrypoint for the systemd service."""
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)-25s | %(message)s",
    )
    proxy = OpenAICompatProxy()
    try:
        asyncio.run(_serve_forever(proxy))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()