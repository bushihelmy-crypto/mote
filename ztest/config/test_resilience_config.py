#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ResilienceConfig (config-v2) + its wiring into the health registry.

``ResilienceConfig`` is the user-facing pydantic surface that maps onto the
framework-agnostic frozen ``BreakerConfig``. Building a :class:`Config` runs a
``model_validator`` that applies the projected breaker config to the shared
process-global registry (so every breaker built thereafter inherits it).
"""
from __future__ import annotations

import pytest

from mote.common.config.config.llm_config import LLMConfig
from mote.common.config.config.models_config import ModelsConfig
from mote.common.config.config.resilience_config import ResilienceConfig
from mote.common.config.meta_config import Config
from mote.common.resilience import BreakerConfig, get_health_registry, reset_health_registry


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_health_registry()
    yield
    reset_health_registry()


def _config(resilience: ResilienceConfig | None = None) -> Config:
    kwargs = {"models": ModelsConfig(default=LLMConfig(api_key="sk-x", model="gpt-4o"))}
    if resilience is not None:
        kwargs["resilience"] = resilience
    return Config(**kwargs)


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
        )
        assert rc.to_breaker_config() == BreakerConfig(
            window_seconds=30.0,
            min_samples=3,
            error_rate_threshold=0.75,
            open_seconds=10.0,
            half_open_max_probes=2,
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
        ],
    )
    def test_rejects_out_of_range(self, field, value):
        with pytest.raises(Exception):
            ResilienceConfig(**{field: value})


class TestWiring:
    def test_config_default_applies_conservative_thresholds(self):
        _config()
        assert get_health_registry()._config == BreakerConfig()

    def test_config_applies_custom_thresholds(self):
        _config(ResilienceConfig(min_samples=9, error_rate_threshold=0.9, open_seconds=5.0))
        cfg = get_health_registry()._config
        assert cfg.min_samples == 9
        assert cfg.error_rate_threshold == 0.9
        assert cfg.open_seconds == 5.0

    def test_disabled_makes_breakers_inert(self):
        _config(ResilienceConfig(enabled=False))
        reg = get_health_registry()
        # An inert breaker always admits and never records → never trips.
        for _ in range(50):
            assert reg.admit("res::x::0") is True
            reg.record("res::x::0", False)
        assert reg.snapshot()["res::x::0"] == "closed"

    def test_config_mutates_existing_registry_in_place(self):
        # A holder that grabbed the registry BEFORE config load must see the new
        # thresholds (set_config mutates in place, does not replace the singleton).
        reg_before = get_health_registry()
        _config(ResilienceConfig(min_samples=7))
        assert reg_before is get_health_registry()
        assert reg_before._config.min_samples == 7
