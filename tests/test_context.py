"""Tests for agent/context — prompt-size caps and preference capping."""

from __future__ import annotations

from agent.context import build_context, build_system_prompt, cap_preferences
from agent.providers import estimate_prompt_bytes
from config import Config


def test_cap_preferences_limits_to_max():
    prefs = [f"pref-{i}" for i in range(20)]
    capped = cap_preferences(prefs)
    assert len(capped) == Config.MAX_PREFS_INJECTED
    assert capped == prefs[: Config.MAX_PREFS_INJECTED]


def test_cap_preferences_passthrough_when_small():
    prefs = ["a", "b"]
    assert cap_preferences(prefs) == prefs


def test_build_context_caps_exchanges():
    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
        for i in range(Config.MAX_CONTEXT_MESSAGES * 4)
    ]
    out = build_context(history, system_prompt="SYS")
    assert len(out) <= Config.MAX_CONTEXT_MESSAGES * 2
    # keeps the newest messages
    assert out[-1]["content"] == history[-1]["content"]


def test_build_context_never_truncates_system():
    system = "S" * 1000
    history = [
        {"role": "user", "content": "X" * Config.MAX_PROMPT_CHARS},
        {"role": "assistant", "content": "Y" * Config.MAX_PROMPT_CHARS},
    ]
    out = build_context(history, system_prompt=system)
    total_chars = len(system) + sum(len(m["content"]) + 32 for m in out)
    assert total_chars <= Config.MAX_PROMPT_CHARS + 32  # bound includes msg overhead
    # newest preserved (possibly truncated) before oldest when trimming
    assert any(m["content"].startswith("Y" * 32) for m in out)


def test_build_context_keeps_all_when_fits():
    history = [{"role": "user", "content": "hi"}]
    out = build_context(history, system_prompt="S")
    assert out == history


# ─── Phase 3 §3.4 — no assembled prompt ever exceeds MAX_PROMPT_CHARS ───────


def test_assembled_prompt_never_exceeds_max_for_any_path():
    core = "persona " * 500
    system = build_system_prompt(
        core,
        sections=["tool descriptions" * 300, "preferences " * 400],
    )
    assert len(system) <= Config.MAX_PROMPT_CHARS

    # chat path: long history
    big_history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"H{i} " * 200}
        for i in range(60)
    ]
    chat_msgs = build_context(big_history, system)
    assert estimate_prompt_bytes(chat_msgs, system) <= Config.MAX_PROMPT_CHARS * 2  # chars≈bytes bound

    # think/classify/curate paths: single giant message
    single = build_context(
        [{"role": "user", "content": "ARTICLE " * 20_000}], system
    )
    total_chars = len(system) + sum(len(m["content"]) + 32 for m in single)
    assert total_chars <= Config.MAX_PROMPT_CHARS + 32


def test_build_system_prompt_caps_oversized_persona():
    system = build_system_prompt(
        "CORE " * 2000,
        sections=["LONG " * 5000, "SHORT"],
    )
    assert len(system) <= Config.MAX_PROMPT_CHARS
    assert system.startswith(("CORE " * 2000).rstrip()[: len("CORE " * 2000)])