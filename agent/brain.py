"""Core AI agent using the tiered model pool (Phase 1).

A registry of free models is faded in/out via the router. Tools are still a
text-JSON contract on the `tools` tier. Failover walks the candidate list,
tracks usage, and trips a circuit breaker under load.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass

from agent.context import build_context, build_system_prompt, cap_preferences
from agent.health import ModelHealth, UsageTracker
from agent.parsing import (
    extract_json_object,
    looks_like_tool_response,
    parse_tool_call,
)
from agent.providers import (
    ProviderError,
    call_model,
    estimate_prompt_bytes,
    shutdown_client,
)
from agent.confirmation import (
    CONFIRMATION_TTL_SECONDS,
    CONFIRMING_TOOLS,
    confirmation_question,
    is_confirmation,
)
from agent.prompts import MAIN_PERSONA, DEEP_THINKING_PROMPT, NEWS_CURATOR_PROMPT
from agent.router import TIER_MAP, route
from agent.tools import ALL_TOOLS, set_database
from config import Config
from services.alerts import alert_owner
from storage.database import Database

logger = logging.getLogger(__name__)

MAX_PER_MODEL_RETRIES = 2        # per candidate, ~3s/6s backoff
FALLBACK_MESSAGE = (
    "⚠️ All my AI backends are temporarily unavailable. Please try again in a few minutes."
)


@dataclass
class ChatResult:
    """Result of a chat() turn, with the metadata Phase 2 feedback needs."""

    text: str
    tool: str | None = None
    tool_args: dict | None = None
    tool_result: str | None = None
    model_id: str | None = None
    tier: str = "chat"

    @property
    def executed_tool(self) -> bool:
        return self.tool is not None

    @property
    def agent_action(self) -> str | None:
        """What the agent did — tool + args (or a reply excerpt)."""
        if not self.tool:
            return (self.text or "")[:200] or None
        args = self.tool_args or {}
        return f"{self.tool}{args}-> {self.tool_result}".strip()

TOOL_POLICY = """
## Tool Use Policy
- If the user asks you to DO something a tool can do (add a task, list tasks, complete a task, save/search/recent notes, set a reminder, get the current date/time, remember a preference, get the news), your FIRST reply MUST be exactly the tool call as a JSON block:
```json {"tool": "tool_name", "args": {"param": "value"}}
```
- Do not describe or acknowledge in prose first — just the JSON block.
- For pure questions that need no action, answer normally in prose.
- Mutating tools (add_task, complete_task, save_note, set_reminder, save_preference) are confirmed with the user by the system before execution — you do NOT need to ask permission yourself, and you must NOT claim you performed an action until you receive a "Tool result:" message.
- After you receive a "Tool result:" message, reply to the user in short prose summarizing the outcome.
"""


def _estimate_output_tokens(result) -> int:
    """Estimate output token count from provider usage when present."""
    usage = getattr(result, "usage", None)
    if usage:
        for key in ("output_tokens", "completion_tokens"):
            if usage.get(key):
                return int(usage[key])
    text = getattr(result, "text", "")
    return max(1, len(text) // 4) if text else 0


class SecondBrain:
    """Manages AI interactions across the tiered model pool."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._chat_history: list[dict] = []
        self._last_model = ("", "")
        self.last_chat: ChatResult | None = None
        self._pending_confirmation: dict | None = None  # tool call awaiting user yes
        self._turn_confirmed = False

        set_database(db)
        self.health = ModelHealth(db)
        self.usage = UsageTracker(db)

    async def start(self) -> None:
        """No eager client; providers build the shared httpx client lazily."""
        logger.info("Second Brain ready (tiered model pool).")

    async def stop(self) -> None:
        await shutdown_client()
        logger.info("AI client stopped.")

    def _build_tool_descriptions(self) -> str:
        """Build a text description of available tools for the system prompt."""
        lines = ["\n## Available Tools\nYou can call these tools by responding with a JSON block. Format:\n```json\n{\"tool\": \"tool_name\", \"args\": {\"param\": \"value\"}}\n```\n"]
        for tool_fn in ALL_TOOLS:
            name = tool_fn.__name__
            doc = tool_fn.__doc__ or "No description"
            lines.append(f"### `{name}`\n{doc}\n")
        return "\n".join(lines)

    async def _generate(
        self,
        tier: str,
        messages: list[dict],
        system_instruction: str,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> str:
        """Generate a reply for `tier`, walking candidates with failover.

        Returns the text, or the FALLBACK_MESSAGE when every candidate fails.
        """
        last_error: Exception | None = None
        exclude: set[str] = set()
        prompt_bytes = estimate_prompt_bytes(messages, system_instruction)

        for cand in await route(tier, self.health, self.usage, exclude):
            self.usage.note_request(cand.id)
            for attempt in range(MAX_PER_MODEL_RETRIES):
                try:
                    result = await call_model(
                        cand,
                        messages,
                        system_instruction,
                        temperature=temperature,
                        max_tokens=max_tokens,
                    )
                    est_out = _estimate_output_tokens(result)
                    await self.usage.record_call(
                        cand.id, prompt_bytes, est_out
                    )
                    await self.health.record_success(cand.id)
                    self._last_model = (cand.id, tier)
                    return result.text
                except ProviderError as e:
                    last_error = e
                    logger.warning(
                        "Model %s failed (attempt %d/%d): %s",
                        cand.id, attempt + 1, MAX_PER_MODEL_RETRIES, str(e)[:120],
                    )
                    await self.usage.record_call(cand.id, prompt_bytes, 0, errored=True)
                    await self.health.record_failure(
                        cand.id, str(e), retryable=e.retryable
                    )
                    if not e.retryable:
                        await alert_owner(
                            f"🚫 Model {cand.id} blocked ({str(e)[:120]}) — "
                            f"hard 6h cooldown.",
                            dedupe_key=f"model-hard-block:{cand.id}",
                        )
                    retryable = e.retryable and attempt + 1 < MAX_PER_MODEL_RETRIES
                    if retryable:
                        backoff = 3 * (attempt + 1)  # 3s, 6s
                        await asyncio.sleep(backoff)
                        continue
                    exclude.add(cand.id)
                    break  # exhausted retries → next candidate

        logger.error(
            "All %s-tier candidates exhausted. Last error: %s",
            tier, last_error,
        )
        return FALLBACK_MESSAGE

    async def _execute_tool(self, tool_name: str, args: dict) -> str | None:
        """Execute an already-parsed tool call; returns the result string."""
        tool_fn = None
        for fn in ALL_TOOLS:
            if fn.__name__ == tool_name:
                tool_fn = fn
                break

        if not tool_fn:
            return f"Tool '{tool_name}' not found."

        try:
            logger.info("Executing tool: %s(%s)", tool_name, args)
            if inspect.iscoroutinefunction(tool_fn):
                result = await tool_fn(**args)
            else:
                result = tool_fn(**args)
            return result
        except Exception as e:
            logger.error("Tool execution error for %s: %s", tool_name, e)
            return f"Error executing tool '{tool_name}': {e}"

    async def chat(self, message: str, confirmed: bool = False) -> ChatResult:
        """Send a message through the tool loop (tools tier).

        Returns a ChatResult carrying the final text plus feedback metadata
        (executed tool, serving model, tier) for Phase 2.

        `confirmed=True` (slash commands) skips the confirmation gate for
        mutating tools — the user already stated explicit intent. Free-text
        messages run the gate: a proposed mutating tool first returns a
        confirmation question and waits for the user to say yes.
        """
        await self.db.log_conversation("user", message)

        preferences = await self.db.get_all_preferences()
        pref_lines = cap_preferences([f"- **{p.key}**: {p.value}" for p in preferences])
        prefs_str = "\n".join(pref_lines)

        pref_section = ""
        if prefs_str:
            pref_section = f"\n## User Habits & Preferences\nYou have learned the following about the user. ALWAYS keep these in mind:\n{prefs_str}\n"

        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Asia/Jakarta"))
        time_section = f"\n## Current Time\n{now.strftime('%A, %B %d, %Y at %H:%M:%S (UTC+7, Asia/Jakarta)')}\n"

        tool_descriptions = self._build_tool_descriptions()
        system_prompt = build_system_prompt(
            MAIN_PERSONA,
            sections=[time_section, pref_section, tool_descriptions + TOOL_POLICY],
        )

        self._chat_history.append({"role": "user", "content": message})
        if len(self._chat_history) > 40:
            self._chat_history = self._chat_history[-40:]

        # ── Confirmation gate ─────────────────────────────────────────────
        self._turn_confirmed = confirmed
        resume: tuple[str, dict] | None = None
        pending = self._pending_confirmation
        self._pending_confirmation = None
        if pending and not confirmed and is_confirmation(message):
            age = time.time() - pending.get("ts", 0)
            if age <= CONFIRMATION_TTL_SECONDS:
                resume = (pending["tool"], pending["args"])
                self._turn_confirmed = True
        # Any other (or expired) pending is dropped here.

        tool_result: str | None = None
        last_tool: str | None = None
        last_tool_args: dict | None = None
        last_tool_result: str | None = None

        # An affirmative reply to a pending question executes it directly —
        # no model round-trip needed for the confirmation itself.
        if resume:
            last_tool, last_tool_args = resume
            tool_result = await self._execute_tool(last_tool, last_tool_args)
            last_tool_result = tool_result
            logger.info("Executing confirmed tool %s %s", last_tool, last_tool_args)
            self._chat_history.append(
                {"role": "assistant", "content": "Action confirmed by the user."}
            )
            self._chat_history.append(
                {"role": "user", "content": f"Tool result:\n{tool_result}"}
            )

        max_iterations = 15
        iteration = 0
        response_text = ""
        strict_retried = False

        while iteration < max_iterations:
            iteration += 1

            if iteration > 1:
                await asyncio.sleep(2)

            tool_result = None
            current_response_text = await self._generate(
                tier=TIER_MAP["chat"],
                messages=build_context(self._chat_history, system_prompt),
                system_instruction=system_prompt,
                temperature=0.3,
            )

            parsed = parse_tool_call(current_response_text)

            # Spec §1.6: on tool-parse failure, one retry with a strict
            # system line; on the second failure, treat as plain prose.
            if parsed is None and looks_like_tool_response(current_response_text) and not strict_retried:
                strict_retried = True
                current_response_text = await self._generate(
                    tier=TIER_MAP["chat"],
                    messages=build_context(self._chat_history, system_prompt),
                    system_instruction=system_prompt
                    + "\nReturn only valid JSON, no prose, no code fences.",
                    temperature=0.2,
                )
                parsed = parse_tool_call(current_response_text)

            if parsed:
                tool_name, args = parsed

                if not self._turn_confirmed and tool_name in CONFIRMING_TOOLS:
                    # Ask before mutating: hold the call, reply with a question.
                    self._pending_confirmation = {
                        "tool": tool_name,
                        "args": args,
                        "ts": time.time(),
                    }
                    question = confirmation_question(tool_name, args)
                    response_text = question or (
                        f"Shall I go ahead with {tool_name}?"
                    )
                    logger.info(
                        "Awaiting confirmation before %s %s", tool_name, args
                    )
                    self._chat_history.append(
                        {"role": "assistant", "content": current_response_text}
                    )
                    self._chat_history.append(
                        {"role": "user", "content": "Awaiting user confirmation."}
                    )
                    break

                tool_result = await self._execute_tool(tool_name, args)
                last_tool = tool_name
                last_tool_args = args
                last_tool_result = tool_result

            if tool_result:
                self._chat_history.append({"role": "assistant", "content": current_response_text})
                self._chat_history.append({"role": "user", "content": f"Tool result:\n{tool_result}"})
            else:
                response_text = current_response_text
                break

        if not response_text:
            response_text = "I couldn't complete the action."

        self._chat_history.append({"role": "assistant", "content": response_text})
        await self.db.log_conversation("assistant", response_text)

        model_id, model_tier = self._last_model
        self.last_chat = ChatResult(
            text=response_text,
            tool=last_tool,
            tool_args=last_tool_args,
            tool_result=last_tool_result,
            model_id=model_id or None,
            tier=model_tier or "chat",
        )
        return self.last_chat

    async def think(self, message: str) -> str:
        """Deep reasoning via the `deep` tier (not part of chat history)."""
        await self.db.log_conversation("user", f"[DEEP THINK] {message}")

        text = await self._generate(
            tier=TIER_MAP["think"],
            messages=[{"role": "user", "content": message}],
            system_instruction=DEEP_THINKING_PROMPT,
            temperature=0.8,
            max_tokens=8192,
        )

        if not text or text == FALLBACK_MESSAGE:
            text = "I couldn't generate a deep analysis. Please try again."

        await self.db.log_conversation("assistant", f"[DEEP THINK] {text}")
        return text

    async def curate_news(self, raw_articles: str) -> str:
        """Curate raw news into a digest via the `chat` tier."""
        prompt = f"""Here are today's raw news articles. Please curate them into a morning digest following your guidelines.

{raw_articles}"""

        text = await self._generate(
            tier=TIER_MAP["curate_news"],
            messages=[{"role": "user", "content": prompt}],
            system_instruction=NEWS_CURATOR_PROMPT,
            temperature=0.5,
        )

        return text or "Could not curate news at this time."

    async def classify_tasks(self, text: str) -> str:
        """Route a free-text into our tool model via the `classify` tier."""
        await self.db.log_conversation("user", f"[CLASSIFY] {text}")
        return await self._generate(
            tier=TIER_MAP["classify"],
            messages=[{"role": "user", "content": text}],
            system_instruction=(
                "Classify the following input into exactly one of: task, reminder, "
                "note, question. Reply with a single JSON object {\"class\": \"...\"}."
            ),
            temperature=0.2,
            max_tokens=32,
        )

    async def classify_correction(
        self, user_message: str, agent_action: str | None
    ) -> dict | None:
        """Explicit-correction classifier (Phase 2 §2.1).

        Lightweight `classify`-tier call answering
        `{"is_correction": bool, "what_was_wrong": str}`. Only ever invoked
        when the previous agent turn contained a tool call. Returns the parsed
        JSON object, or None when the message was not a correction / undecided.
        """
        prompt = (
            "A user message follows an assistant action. Decide whether the user is "
            "correcting the assistant about what it just did (wrong task, wrong details, "
            "missing info, right intent but you did the wrong thing). Pure questions and "
            "follow-through instructions like 'thanks' or 'also add milk' are NOT corrections. "
            "Reply with a single JSON object:\n"
            '{"is_correction": true|false, "what_was_wrong": "one sentence"}\n\n'
            f'Assistant action: {agent_action or "(none)"}\n\n'
            f'User message: {user_message[:500]}'
        )
        text = await self._generate(
            tier=TIER_MAP["classify"],
            messages=[{"role": "user", "content": prompt}],
            system_instruction="Return only valid JSON, no prose, no code fences.",
            temperature=0.1,
            max_tokens=80,
        )
        obj = extract_json_object(text)
        if not obj or not isinstance(obj.get("is_correction"), bool):
            return None
        if obj["is_correction"]:
            return {
                "is_correction": True,
                "what_was_wrong": str(obj.get("what_was_wrong") or "").strip()
                or "(no detail)",
            }
        return {"is_correction": False, "what_was_wrong": None}