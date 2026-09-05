"""Model registry — all model definitions as data.

Loaded at startup with a curated seed set. The whole registry is overridable
via the MODELS_FILE env var pointing at a YAML or JSON file. Correcting a
stale model ID or quota is a config edit, never a code change.

Env overrides honoured with priority 0:
- FAST_MODEL / DEEP_MODEL  (existing behaviour: whichever model the .env pins)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from config import Config

logger = logging.getLogger(__name__)

TIERS = {"classify", "chat", "tools", "deep", "vision", "audio"}


@dataclass(frozen=True)
class ModelSpec:
    id: str                            # provider's model identifier
    provider: str                      # zen | google | groq | cerebras | openrouter | github
    base_url: str
    api_style: str                     # "responses" | "chat_completions"
    api_key_env: str                   # name of the env var holding the key
    tiers: frozenset[str]              # subset of TIERS
    priority: int                      # lower = preferred within the pool
    rpm: int | None = None             # requests per minute cap, if known
    rpd: int | None = None             # requests per day cap, if known
    max_output_tokens: int = 4096


# ─── Seed registry ────────────────────────────────────────────────────────────
# Verified September 2026. Free tiers rotate; the registry is meant to be edited.
_ZEN_URL = "https://opencode.ai/zen/v1"
_GOOGLE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
_GROQ_URL = "https://api.groq.com/openai/v1"
_CEREBRAS_URL = "https://api.cerebras.ai/v1"
_OPENROUTER_URL = "https://openrouter.ai/api/v1"
_GITHUB_URL = "https://models.inference.ai.azure.com"

DEFAULT_REGISTRY: list[ModelSpec] = [
    # ── zen (existing key) — primary, free ─────────────────────────────────
    ModelSpec(
        id="muse-spark-1.3-contributor-free",
        provider="zen",
        base_url=_ZEN_URL,
        api_style="responses",
        api_key_env="OPENCODE_ZEN_API_KEY",
        tiers=frozenset({"classify", "chat", "tools", "deep"}),
        priority=0,
        rpm=None,
        rpd=None,
        max_output_tokens=8192,
    ),
    ModelSpec(
        id="big-pickle",
        provider="zen",
        base_url=_ZEN_URL,
        api_style="chat_completions",
        api_key_env="OPENCODE_ZEN_API_KEY",
        tiers=frozenset({"classify", "chat"}),
        priority=5,
        rpm=None,
        rpd=None,
        max_output_tokens=8192,
    ),
    ModelSpec(
        id="mimo-v2.5-free",
        provider="zen",
        base_url=_ZEN_URL,
        api_style="chat_completions",
        api_key_env="OPENCODE_ZEN_API_KEY",
        tiers=frozenset({"classify", "chat"}),
        priority=5,
        rpm=None,
        rpd=None,
        max_output_tokens=8192,
    ),
    ModelSpec(
        id="ling-3.0-flash-fin-free",
        provider="zen",
        base_url=_ZEN_URL,
        api_style="chat_completions",
        api_key_env="OPENCODE_ZEN_API_KEY",
        tiers=frozenset({"classify", "chat"}),
        priority=6,
        rpm=None,
        rpd=None,
        max_output_tokens=8192,
    ),
    ModelSpec(
        id="nemotron-3.5-lightning-free",
        provider="zen",
        base_url=_ZEN_URL,
        api_style="chat_completions",
        api_key_env="OPENCODE_ZEN_API_KEY",
        tiers=frozenset({"classify", "chat"}),
        priority=8,
        rpm=None,
        rpd=None,
        max_output_tokens=8192,
    ),
    # ── google (AI Studio) — generous free tier ───────────────────────────
    ModelSpec(
        id="gemini-2.5-flash",
        provider="google",
        base_url=_GOOGLE_URL,
        api_style="chat_completions",
        api_key_env="GEMINI_API_KEY",
        tiers=frozenset({"classify", "chat", "deep", "vision"}),
        priority=10,
        rpm=15,
        rpd=1500,
        max_output_tokens=8192,
    ),
    # ── groq — fast, minute-capped ─────────────────────────────────────────
    ModelSpec(
        id="llama-3.3-70b-versatile",
        provider="groq",
        base_url=_GROQ_URL,
        api_style="chat_completions",
        api_key_env="GROQ_API_KEY",
        tiers=frozenset({"classify", "chat", "deep"}),
        priority=20,
        rpm=30,
        rpd=1000,
        max_output_tokens=8192,
    ),
    ModelSpec(
        id="llama-3.1-8b-instant",
        provider="groq",
        base_url=_GROQ_URL,
        api_style="chat_completions",
        api_key_env="GROQ_API_KEY",
        tiers=frozenset({"classify", "chat"}),
        priority=21,
        rpm=30,
        rpd=14400,
        max_output_tokens=4096,
    ),
    ModelSpec(
        id="whisper-large-v3-turbo",
        provider="groq",
        base_url=_GROQ_URL,
        api_style="chat_completions",  # audio handled separately in Phase 5
        api_key_env="GROQ_API_KEY",
        tiers=frozenset({"audio"}),
        priority=30,
        rpm=20,
        rpd=2000,
        max_output_tokens=512,
    ),
    # ── openrouter — backstop only ─────────────────────────────────────────
    ModelSpec(
        id="openai/gpt-oss-120b:free",
        provider="openrouter",
        base_url=_OPENROUTER_URL,
        api_style="chat_completions",
        api_key_env="OPENROUTER_API_KEY",
        tiers=frozenset({"classify", "chat", "deep"}),
        priority=40,
        rpm=20,
        rpd=50,
        max_output_tokens=8192,
    ),
    ModelSpec(
        id="openai/gpt-oss-20b:free",
        provider="openrouter",
        base_url=_OPENROUTER_URL,
        api_style="chat_completions",
        api_key_env="OPENROUTER_API_KEY",
        tiers=frozenset({"classify", "chat"}),
        priority=41,
        rpm=20,
        rpd=50,
        max_output_tokens=8192,
    ),
    # ── github (GitHub Models) — free PAT, low caps ───────────────────────
    ModelSpec(
        id="gpt-4o-mini",
        provider="github",
        base_url=_GITHUB_URL,
        api_style="chat_completions",
        api_key_env="GITHUB_TOKEN",
        tiers=frozenset({"classify", "chat"}),
        priority=50,
        rpm=15,
        rpd=150,
        max_output_tokens=4096,
    ),
    # ── cerebras — backstop; free tier replaced by paid trial (Jul 2026) ──
    ModelSpec(
        id="gpt-oss-120b",
        provider="cerebras",
        base_url=_CEREBRAS_URL,
        api_style="chat_completions",
        api_key_env="CEREBRAS_API_KEY",
        tiers=frozenset({"classify", "chat", "deep"}),
        priority=60,
        rpm=30,
        rpd=None,
        max_output_tokens=4096,
    ),
]


def _api_key_for(spec: ModelSpec) -> str:
    """Return the API key for a model spec (possibly empty)."""
    return os.environ.get(spec.api_key_env, "")


def _from_dict(d: dict) -> ModelSpec:
    tiers_raw = d["tiers"]
    if isinstance(tiers_raw, str):
        tiers = frozenset(t.strip() for t in tiers_raw.split(",") if t.strip())
    else:
        tiers = frozenset(tiers_raw)
    unknown = tiers - TIERS
    if unknown:
        raise ValueError(f"Unknown tiers {sorted(unknown)} in model '{d.get('id')}'")
    return ModelSpec(
        id=d["id"],
        provider=d["provider"],
        base_url=d["base_url"],
        api_style=d["api_style"],
        api_key_env=d["api_key_env"],
        tiers=tiers,
        priority=int(d.get("priority", 999)),
        rpm=d.get("rpm"),
        rpd=d.get("rpd"),
        max_output_tokens=int(d.get("max_output_tokens", 4096)),
    )


def _load_registry_file(path: str | Path) -> list[ModelSpec]:
    text = Path(path).read_text(encoding="utf-8").strip()
    if not text.startswith("{"):
        import yaml

        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("models", [])
    return [_from_dict(d) for d in data]


def load_registry() -> list[ModelSpec]:
    """Build the active registry.

    Uses MODELS_FILE if set, otherwise the seed set. Then applies the
    FAST_MODEL / DEEP_MODEL env overrides as priority-0 entries so an existing
    .env continues to behave predictably.
    """
    if Config.MODELS_FILE:
        specs = _load_registry_file(Config.MODELS_FILE)
        logger.info("Loaded %d models from MODELS_FILE=%s", len(specs), Config.MODELS_FILE)
    else:
        specs = list(DEFAULT_REGISTRY)

    # De-duplicate by id, keeping the first occurrence (seed or file order).
    by_id: dict[str, ModelSpec] = {}
    order: list[str] = []
    for spec in specs:
        if spec.id not in by_id:
            by_id[spec.id] = spec
            order.append(spec.id)

    # FAST_MODEL override → priority-0 for classify/chat/tools (zen).
    if Config.FAST_MODEL != "muse-spark-1.3-contributor-free" or not any(
        s.id == Config.FAST_MODEL for s in by_id.values()
    ):
        fast = ModelSpec(
            id=Config.FAST_MODEL,
            provider="zen",
            base_url=Config.MODEL_API_URL,
            api_style="responses",
            api_key_env="OPENCODE_ZEN_API_KEY",
            tiers=frozenset({"classify", "chat", "tools"}),
            priority=-10,
            max_output_tokens=8192,
        )
        if fast.id not in by_id:
            by_id[fast.id] = fast
            order.insert(0, fast.id)

    # DEEP_MODEL override → priority-0 for deep (zen).
    if not any(s.id == Config.DEEP_MODEL for s in by_id.values()):
        deep = ModelSpec(
            id=Config.DEEP_MODEL,
            provider="zen",
            base_url=Config.MODEL_API_URL,
            api_style="responses",
            api_key_env="OPENCODE_ZEN_API_KEY",
            tiers=frozenset({"deep"}),
            priority=-10,
            max_output_tokens=8192,
        )
        if deep.id not in by_id:
            by_id[deep.id] = deep
            order.insert(0 if Config.DEEP_MODEL == Config.FAST_MODEL else len(order), deep.id)

    return [by_id[i] for i in order]


def get_registry() -> list[ModelSpec]:
    """Return the currently active registry (sorted by priority)."""
    return sorted(load_registry(), key=lambda s: (s.priority, s.id))