#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ResilienceConfig (config-v2) + its wiring into the health registry.

``ResilienceConfig`` is the user-facing pydantic surface that maps onto the
framework-agnostic frozen ``BreakerConfig``. Parsing remains pure; the Runtime
Context composition boundary applies it to the Engine-owned registry.
"""

from __future__ import annotations

import pytest

from mote.contracts.config.runtime_client import RuntimeClientActivationSpec
from mote.product.config.model.inputs import ProductEndpointInput, ShortcutModelsConfig
from mote.product.config.resilience import ResilienceConfig
from mote.product.config.schema import Config
from mote.runtime.models.clients.context import Context
from mote.runtime.resilience import BreakerConfig, ResourceHealthRegistry


def _context(resilience: ResilienceConfig | None = None) -> Context:
    kwargs = {"models": ShortcutModelsConfig(default=ProductEndpointInput(api_key="sk-x", model="gpt-4o"))}
    if resilience is not None:
        kwargs["resilience"] = resilience
    config = Config(**kwargs)
    return Context(activation=RuntimeClientActivationSpec(breaker=config.resilience.to_breaker_config()))


class TestResilienceConfig:
    def test_defaults_match_primitive_defaults(self):
        # The config default must equal the leaf primitive default (single source
        # of truth for "conservative" thresholds).
        assert ResilienceConfig().to_breaker_config() == BreakerConfig()

    def test_to_breaker_config_projects_all_fields(self):
        rc = ResilienceConfig(
            enabled=False,
            window_seconds=30.0,
            min_samples=3,
            error_rate_threshold=0.75,
            open_seconds=10.0,
            half_open_max_probes=2,
            half_open_success_quorum=2,
            probe_grace_seconds=0.5,
        )
        assert rc.to_breaker_config() == BreakerConfig(
            window_seconds=30.0,
            min_samples=3,
            error_rate_threshold=0.75,
            open_seconds=10.0,
            half_open_max_probes=2,
            half_open_success_quorum=2,
            probe_grace_seconds=0.5,
            enabled=False,
        )

    @pytest.mark.parametrize(
        "field,value",
        [
            ("window_seconds", 0.0),
            ("min_samples", 0),
            ("error_rate_threshold", 1.5),
            ("error_rate_threshold", -0.1),
            ("open_seconds", 0.0),
            ("half_open_max_probes", 0),
            ("half_open_success_quorum", 0),
            ("probe_grace_seconds", -0.1),
        ],
    )
    def test_rejects_out_of_range(self, field, value):
        with pytest.raises(Exception):
            ResilienceConfig(**{field: value})

    def test_rejects_quorum_above_probe_limit(self):
        with pytest.raises(Exception):
            ResilienceConfig(
                half_open_max_probes=1,
                half_open_success_quorum=2,
            )


class TestWiring:
    def test_parsing_config_does_not_mutate_runtime_registry(self):
        reg = ResourceHealthRegistry()
        Config(
            models=ShortcutModelsConfig(default=ProductEndpointInput(api_key="sk-x", model="gpt-4o")),
            resilience=ResilienceConfig(min_samples=9),
        )
        assert reg._config == BreakerConfig()

    def test_config_default_applies_conservative_thresholds(self):
        assert _context().health_registry._config == BreakerConfig()

    def test_config_applies_custom_thresholds(self):
        cfg = _context(
            ResilienceConfig(min_samples=9, error_rate_threshold=0.9, open_seconds=5.0)
        ).health_registry._config
        assert cfg.min_samples == 9
        assert cfg.error_rate_threshold == 0.9
        assert cfg.open_seconds == 5.0

    def test_disabled_makes_breakers_inert(self):
        reg = _context(ResilienceConfig(enabled=False)).health_registry
        # An inert breaker always admits and never records → never trips.
        for _ in range(50):
            assert reg.admit("res::x::0") is True
            reg.record("res::x::0", False)
        assert reg.snapshot()["res::x::0"] == "closed"

    def test_contexts_do_not_share_breaker_state(self):
        first = _context(ResilienceConfig(min_samples=7))
        second = _context(ResilienceConfig(min_samples=3))
        assert first.health_registry is not second.health_registry
        assert first.health_registry._config.min_samples == 7
        assert second.health_registry._config.min_samples == 3
