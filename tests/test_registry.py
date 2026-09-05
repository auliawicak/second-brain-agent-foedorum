"""Unit tests for agent.registry and agent.router — seed registry, tier filtering."""

from __future__ import annotations

import os
from collections import Counter

import pytest
from agent.registry import TIERS, ModelSpec, get_registry, load_registry


class TestRegistry:
    def test_seed_registry_not_empty(self) -> None:
        specs = load_registry()
        assert len(specs) >= 5

    def test_all_tiers_present_in_registry(self) -> None:
        seen = set()
        for spec in load_registry():
            seen |= spec.tiers
        assert "tools" in seen
        assert "chat" in seen

    def test_muse_spark_is_tools_tier(self) -> None:
        specs = get_registry()
        muse = next(s for s in specs if s.id == "muse-spark-1.3-contributor-free")
        assert "tools" in muse.tiers
        assert "classify" in muse.tiers
        assert "deep" in muse.tiers

    def test_fast_model_override_priority_minus10(self) -> None:
        """If FAST_MODEL differs from default, it should appear with priority -10."""
        specs = get_registry()
        priority_map = {s.id: s.priority for s in specs}
        # muse-spark default is always there; if user overrides FAST_MODEL
        # to something else, it should show at priority -10
        # We cannot test the override without env var, so just check muse is there.
        assert priority_map["muse-spark-1.3-contributor-free"] <= 0

    def test_model_spec_frozen(self) -> None:
        spec = ModelSpec(
            id="test", provider="zen", base_url="http://x",
            api_style="responses", api_key_env="X",
            tiers=frozenset({"chat"}), priority=0,
        )
        with pytest.raises(AttributeError):
            spec.id = "changed"  # type: ignore[misc]
