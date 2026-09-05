"""Tier-aware model routing (Phase 1).

Each request has an abstract `tier`: classify, chat, tools, deep. The router
returns an ordered list of eligible models, excluding any that are currently
blocked (open breaker), over their RPM window, or over 80% of their daily
budget. It also folds in the `exclude` set so the brain can skip models that
already failed on this specific request.
"""

from __future__ import annotations

import logging

from agent.health import ModelHealth, UsageTracker
from agent.registry import ModelSpec, get_registry

logger = logging.getLogger(__name__)

# Map each brain entry point to the tier its prompt targets.
TIER_MAP: dict[str, str] = {
    "chat": "tools",          # chat() drives the tool loop
    "think": "deep",
    "curate_news": "chat",
    "classify": "classify",
}


def models_for_tier(tier: str, registry: list[ModelSpec]) -> list[ModelSpec]:
    """All registry models that serve the tier, lowest priority first."""
    return [spec for spec in registry if tier in spec.tiers]


async def route(
    tier: str,
    health: ModelHealth,
    usage: UsageTracker,
    exclude: set[str] | None = None,
) -> list[ModelSpec]:
    """Return an ordered, currently-eligible model list for the tier."""
    import os
    exclude = exclude or set()
    eligible: list[ModelSpec] = []
    for spec in get_registry():
        if tier not in spec.tiers:
            continue
        if spec.id in exclude:
            continue
        if not os.environ.get(spec.api_key_env, ""):
            continue
        if await health.is_blocked(spec.id):
            continue
        if usage.rpm_full(spec):
            continue
        if await usage.at_rpd_budget(spec):
            continue
        eligible.append(spec)
    eligible.sort(key=lambda s: (s.priority, s.id))
    return eligible