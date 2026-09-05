"""Core AI agent using the OpenCode Zen gateway (OpenAI Responses API).

Powering a 24/7 second brain. The default model is Muse Spark 1.3
Contributor Free (`muse-spark-1.3-contributor-free`), used across all modes:

- `chat()`: Everyday interactions with the user
- `think()`: Complex analysis and multi-step reasoning
- `curate_news()`: Morning news digest generation
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time

from openai import AsyncOpenAI

from agent.context import build_context, build_system_prompt, cap_preferences
from agent.prompts import MAIN_PERSONA, DEEP_THINKING_PROMPT, NEWS_CURATOR_PROMPT
from agent.tools import ALL_TOOLS, set_database
from config import Config
from storage.database import Database

logger = logging.getLogger(__name__)


class SecondBrain:
    """Manages AI interactions via the OpenCode Zen gateway.

    Model configurations:
    - `chat()`: Uses the configured model for everyday interactions
    - `think()`: Uses the configured model for deep reasoning
    - `curate_news()`: Uses the configured model with a news curator prompt
    """

    def __init__(self, db: Database) -> None:
        self.db = db
        self._client: AsyncOpenAI | None = None
        self._chat_history: list[dict] = []

        # Inject database into tools module
        set_database(db)

    async def start(self) -> None:
        """Initialize the OpenCode Zen client."""
        logger.info("Starting Second Brain AI client (OpenCode Zen)...")
        self._client = AsyncOpenAI(
            api_key=Config.OPENCODE_ZEN_API_KEY,
            base_url=Config.MODEL_API_URL,
        )
        logger.info("AI client ready.")

    async def stop(self) -> None:
        """Clean up."""
        self._client = None
        logger.info("AI client stopped.")

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            raise RuntimeError("AI client not started. Call start() first.")
        return self._client

    def _build_tool_descriptions(self) -> str:
        """Build a text description of available tools for the system prompt."""
        lines = ["\n## Available Tools\nYou can call these tools by responding with a JSON block. Format:\n```json\n{\"tool\": \"tool_name\", \"args\": {\"param\": \"value\"}}\n```\n"]
        for tool_fn in ALL_TOOLS:
            name = tool_fn.__name__
            doc = tool_fn.__doc__ or "No description"
            lines.append(f"### `{name}`\n{doc}\n")
        return "\n".join(lines)

    async def _generate_with_retry(
        self,
        messages: list[dict],
        system_instruction: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        max_retries: int = 3,
    ) -> str:
        """Generate content with retry logic.

        Uses the OpenAI Responses API (as required by OpenCode Zen for
        muse-spark-1.3-contributor-free). Retries on rate-limit (429) and
        overload (503) errors.
        """
        last_error = None

        for attempt in range(max_retries):
            try:
                response = await self.client.responses.create(
                    model=Config.FAST_MODEL,
                    input=messages,
                    instructions=system_instruction,
                    temperature=temperature,
                    max_output_tokens=max_tokens,
                )
                return response.output_text or ""
            except Exception as e:
                last_error = e
                error_str = str(e)
                # Retry on 503 (overloaded) or 429 (rate limited)
                if "503" in error_str or "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                    if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                        wait_time = (4 ** attempt) + 2  # 3s, 6s, 18s
                    else:
                        wait_time = (2 ** attempt) + 1  # 2s, 3s, 5s
                    logger.warning(
                        "Model returned %s (attempt %d/%d). Retrying in %ds...",
                        error_str[:80], attempt + 1, max_retries, wait_time
                    )
                    await asyncio.sleep(wait_time)
                else:
                    # Non-retryable error
                    logger.error("Model error (non-retryable): %s", error_str[:120])
                    break

        # All attempts failed
        logger.error("All attempts failed. Last error: %s", last_error)
        return "⚠️ I'm having trouble connecting to my AI backend right now. Please try again in a minute."

    async def _process_tool_calls(self, response_text: str) -> str | None:
        """Check if the response contains a tool call and execute it.

        Returns the tool result, or None if no tool call was found.
        """
        import json
        import re

        # Extract JSON from code blocks first, then try bare JSON
        # Pattern 1: fenced code block
        code_block_match = re.search(
            r'```(?:json)?\s*\n(.*?)\n\s*```', response_text, re.DOTALL
        )
        # Pattern 2: bare JSON object with "tool" key
        bare_json_match = re.search(
            r'(\{\s*"tool"\s*:.*)', response_text, re.DOTALL
        )

        raw_json = None
        if code_block_match:
            raw_json = code_block_match.group(1).strip()
        elif bare_json_match:
            raw_json = bare_json_match.group(1).strip()

        if not raw_json:
            return None

        # Try to parse the JSON, handling common issues
        try:
            call = json.loads(raw_json)
        except json.JSONDecodeError:
            # The model might have extra text after the JSON. Try to
            # extract just the first complete JSON object.
            brace_depth = 0
            end_idx = -1
            for i, ch in enumerate(raw_json):
                if ch == '{':
                    brace_depth += 1
                elif ch == '}':
                    brace_depth -= 1
                    if brace_depth == 0:
                        end_idx = i + 1
                        break
            if end_idx > 0:
                try:
                    call = json.loads(raw_json[:end_idx])
                except json.JSONDecodeError as e:
                    logger.error("Tool JSON parse error: %s | raw: %s", e, raw_json[:200])
                    return f"Error parsing tool call: {e}"
            else:
                logger.error("Could not find complete JSON object in: %s", raw_json[:200])
                return None

        tool_name = call.get("tool")
        args = call.get("args", {})

        if not tool_name:
            return None

        # Find the tool function
        tool_fn = None
        for fn in ALL_TOOLS:
            if fn.__name__ == tool_name:
                tool_fn = fn
                break

        if not tool_fn:
            return f"Tool '{tool_name}' not found."

        try:
            logger.info("Executing tool: %s(%s)", tool_name, args)
            # Handle both sync and async tools
            if inspect.iscoroutinefunction(tool_fn):
                result = await tool_fn(**args)
            else:
                result = tool_fn(**args)
            return result
        except Exception as e:
            logger.error("Tool execution error for %s: %s", tool_name, e)
            return f"Error executing tool '{tool_name}': {e}"

    async def chat(self, message: str) -> str:
        """Send a message using the configured model and get a response.

        Handles tool calls automatically — if the model requests a tool,
        executes it and sends the result back for a final response.
        """
        # Log the user message
        await self.db.log_conversation("user", message)

        # Load user preferences (capped at MAX_PREFS_INJECTED)
        preferences = await self.db.get_all_preferences()
        pref_lines = cap_preferences([f"- **{p.key}**: {p.value}" for p in preferences])
        prefs_str = "\n".join(pref_lines)

        pref_section = ""
        if prefs_str:
            pref_section = f"\n## User Habits & Preferences\nYou have learned the following about the user. ALWAYS keep these in mind:\n{prefs_str}\n"

        # Inject current time so the model doesn't need to call get_current_datetime
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Asia/Jakarta"))
        time_section = f"\n## Current Time\n{now.strftime('%A, %B %d, %Y at %H:%M:%S (UTC+7, Asia/Jakarta)')}\n"

        tool_descriptions = self._build_tool_descriptions()
        system_prompt = build_system_prompt(
            MAIN_PERSONA,
            sections=[time_section, pref_section, tool_descriptions],
        )

        # Add user message to history
        self._chat_history.append({"role": "user", "content": message})

        # Keep stored history manageable (last 40 messages)
        if len(self._chat_history) > 40:
            self._chat_history = self._chat_history[-40:]

        max_iterations = 15
        iteration = 0
        response_text = ""

        while iteration < max_iterations:
            iteration += 1

            # Small delay between iterations to respect rate limits
            if iteration > 1:
                await asyncio.sleep(2)

            current_response_text = await self._generate_with_retry(
                messages=build_context(self._chat_history, system_prompt),
                system_instruction=system_prompt,
                temperature=0.7,
            )

            # Check for tool calls
            tool_result = await self._process_tool_calls(current_response_text)

            if tool_result:
                # Add the model's tool call and result to history
                self._chat_history.append({"role": "assistant", "content": current_response_text})
                self._chat_history.append({"role": "user", "content": f"Tool result:\n{tool_result}"})
            else:
                response_text = current_response_text
                break

        if not response_text:
            response_text = "I couldn't complete the action."

        # Add final response to history
        self._chat_history.append({"role": "assistant", "content": response_text})

        # Log the assistant response
        await self.db.log_conversation("assistant", response_text)

        return response_text

    async def think(self, message: str) -> str:
        """Send a message to the configured reasoning model.

        Used for complex analysis, research, and multi-step reasoning.
        Not part of the ongoing chat history.
        """
        await self.db.log_conversation("user", f"[DEEP THINK] {message}")

        text = await self._generate_with_retry(
            messages=[{"role": "user", "content": message}],
            system_instruction=DEEP_THINKING_PROMPT,
            temperature=0.8,
            max_tokens=8192,
        )

        if not text:
            text = "I couldn't generate a deep analysis. Please try again."

        await self.db.log_conversation("assistant", f"[DEEP THINK] {text}")
        return text

    async def curate_news(self, raw_articles: str) -> str:
        """Send raw news articles to be curated into a digest.

        Args:
            raw_articles: Raw news article data (titles, descriptions, URLs).

        Returns:
            A formatted, curated news digest.
        """
        prompt = f"""Here are today's raw news articles. Please curate them into a morning digest following your guidelines.

{raw_articles}"""

        text = await self._generate_with_retry(
            messages=[{"role": "user", "content": prompt}],
            system_instruction=NEWS_CURATOR_PROMPT,
            temperature=0.5,
        )

        return text or "Could not curate news at this time."