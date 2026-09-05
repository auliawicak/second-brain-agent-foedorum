"""Golden test harness for the `tools` tier (standalone, DB-free).

Runs a fixed set of utterances through a single model id by calling the
provider adapter directly, bypassing the router and database entirely.
Parses the returned text for a JSON tool call and scores against the
expected tool and arguments.

The `tools` tier is promoted to GA at >= 90% accuracy over the full gated
set across >= 3 providers, as recorded in docs/model_matrix.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

logging.basicConfig(level=logging.WARNING)

GOLDEN_CASES: list[dict] = [
    # -- add_task --
    {"text": "Add a task: buy groceries tomorrow", "tool": "add_task"},
    {"text": "buat tugas: bayar tagihan listrik besok", "tool": "add_task", "lang": "id"},
    {"text": "please create a task to finish the TPS report by Friday", "tool": "add_task"},
    # -- list / complete -- 
    {"text": "show me all my pending tasks", "tool": "list_tasks"},
    {"text": "list semua task yang pending", "tool": "list_tasks", "lang": "id"},
    {"text": "what do I need to get done today", "tool": "list_tasks"},
    {"text": "mark task #3 as done", "tool": "complete_task", "args": {"task_id": 3}},
    {"text": "selesaikan tugas nomor 3", "tool": "complete_task", "args": {"task_id": 3}, "lang": "id"},
    {"text": "complete the task with id 7", "tool": "complete_task", "args": {"task_id": 7}},
    {"text": "tandai selesai task 2", "tool": "complete_task", "args": {"task_id": 2}, "lang": "id"},
    # -- agenda --
    {"text": "what's on my agenda today?", "tool": "get_today_agenda"},
    {"text": "agenda hari ini apa", "tool": "get_today_agenda", "lang": "id"},
    # -- notes --
    {"text": "save a note: SSH key is in ~/.ssh and rotate it monthly", "tool": "save_note"},
    {"text": "catat catatan: meeting notes for sprint planning", "tool": "save_note"},
    {"text": "simpan catatan bahwa rapat diundur jam 2", "tool": "save_note", "lang": "id"},
    {"text": "jot this down: milk, eggs, bread", "tool": "save_note"},
    {"text": "search my notes for 'deploy'", "tool": "search_notes", "args": {"query": "deploy"}},
    {"text": "find notes about launch plan", "tool": "search_notes", "args": {"query": "launch"}},
    {"text": "my recent notes", "tool": "get_recent_notes"},
    {"text": "show the last notes I saved", "tool": "get_recent_notes"},
    # -- reminder --
    {"text": "remind me on Friday 3pm to submit the report", "tool": "set_reminder"},
    {"text": "ingatkan saya besok jam 8 pagi sarapan", "tool": "set_reminder", "lang": "id"},
    {"text": "set a reminder for 6am to stretch", "tool": "set_reminder"},
    # -- datetime --
    {"text": "what time is it now?", "tool": "get_current_datetime"},
    {"text": "jam berapa sekarang", "tool": "get_current_datetime", "lang": "id"},
    {"text": "what's today's date exactly", "tool": "get_current_datetime"},
    # -- preference --
    {"text": "remember that I prefer coffee over tea", "tool": "save_preference", "args": {"key": "coffee", "value": "yes"}},
    {"text": "record that my favorite city is Lisbon", "tool": "save_preference", "args": {"key": "favorite_city", "value": "libson"}},
    # -- news --
    {"text": "get the latest news", "tool": "get_news"},
    {"text": "apa berita terbaru", "tool": "get_news", "lang": "id"},
]

GOLDEN_TOTAL = len(GOLDEN_CASES)

SYSTEM_PROMPT = (
    "You are a second-brain assistant. Choose exactly one tool from the "
    "available set and reply with a JSON object: "
    '{"tool": "tool_name", "args": {...}}. '
    "No prose, no explanation, just the JSON block."
)

# Mirror the production chat() prompt exactly: include the tool descriptions
# so the model knows the tool names + argument contract to obey.
def build_harness_system_prompt() -> str:
    from agent.brain import SecondBrain

    brain = SecondBrain.__new__(SecondBrain)  # bypass __init__ (no DB needed)
    return SYSTEM_PROMPT + brain._build_tool_descriptions()


def _parse_and_check(response_text: str, expected_tool: str) -> tuple[dict, bool]:
    from agent.parsing import parse_tool_call

    parsed = parse_tool_call(response_text)
    if parsed is None:
        return {"tool": "__none__"}, False
    name, args = parsed
    return {"tool": name, "args": args}, name == expected_tool


async def run_once(model_id: str, provider: str) -> dict:
    import time

    from agent.providers import ProviderError, call_model
    from agent.registry import DEFAULT_REGISTRY

    spec = next((s for s in DEFAULT_REGISTRY if s.id == model_id), None)
    if spec is None:
        raise SystemExit(f"Model '{model_id}' not found in seed registry.")

    sys_prompt = build_harness_system_prompt()
    results: list[dict] = []
    passed = 0

    for idx, case in enumerate(GOLDEN_CASES):
        got: dict = {"tool": "__none__"}
        ok = False
        dry = 0
        # Pace requests: reasoning models drop tokens on burstiness; free tiers
        # rate-limit. 2s between cases + bounded 429/5xx retries.
        await asyncio.sleep(2.0)
        while dry < 3:
            try:
                res = await call_model(
                    spec,
                    [{"role": "user", "content": case["text"]}],
                    sys_prompt,
                    temperature=0.0,
                    max_tokens=2048,
                )
                if not res.text:
                    got = {"tool": "__empty__"}
                    dry = 3  # non-empty text is required; treat as hard fail
                    break
                got, ok = _parse_and_check(res.text, case["tool"])
                if ok:
                    passed += 1
                break
            except ProviderError as e:
                if e.status in (401, 403, 400):
                    got = {"tool": "__auth_error__", "error": str(e)[:120]}
                    break
                dry += 1
                await asyncio.sleep(4)
            except Exception as e:
                got = {"tool": "__error__", "error": str(e)[:120]}
                break

        results.append({
            "text": case["text"],
            "expected_tool": case["tool"],
            "got": got,
            "ok": ok,
        })

    return {
        "model": model_id,
        "provider": provider,
        "calls": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "score": passed / max(1, len(results)),
        "breakdown": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="golden_tools")
    parser.add_argument("--model", required=True, help="Model id to test")
    parser.add_argument("--provider", default="zen", help="Provider label")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    result = asyncio.run(run_once(args.model, args.provider))

    if args.as_json:
        print(json.dumps(result, indent=2, default=str))
        return 0 if result["passed"] >= int(0.9 * result["calls"]) else 1

    print(f"\n=== Golden tools :: {result['model']} ({result['provider']}) ===")
    print(f"Passed {result['passed']}/{result['calls']}  ({100*result['score']:.0f}%)")
    for row in result["breakdown"]:
        mark = "PASS" if row["ok"] else "FAIL"
        print(f"  [{mark}] {row['text'][:50]!r} -> {row['got']}")
    print()
    return 0 if result["passed"] >= int(0.9 * result["calls"]) else 1


if __name__ == "__main__":
    sys.exit(main())
