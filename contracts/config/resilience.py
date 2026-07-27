#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Resilience settings — the circuit-breaker knobs, in the config-v2 surface.

This pydantic model is the *user-facing* half of the resilience primitive: it is
validated, YAML-serializable, and composed into the top-level :class:`Config`.
It maps onto the framework-agnostic frozen :class:`~mote.contracts.resilience.BreakerConfig`
(the leaf primitive any subsystem constructs a breaker from) via
:meth:`to_breaker_config`. The primitive layer never imports config-v2; the
dependency points one way (config → primitive), so the state machine stays a
domain-agnostic leaf.
"""
from __future__ import annotations

from pydantic import Field, model_validator

from mote.contracts.config.base import ConfigModel as YamlModel
from mote.contracts.resilience import BreakerConfig


class ResilienceConfig(YamlModel):
    """Circuit-breaker parameters for per-resource health gating.

    A breaker sheds calls to a resource (an LLM ``api_type::model::key_index``,
    and later MCP servers / egress hosts) once it has been failing enough, and
    lets the recovery loop fail over to a healthy one. Defaults are deliberately
    conservative so a breaker only trips on *sustained* failure and recovers
    quickly. Turn the whole mechanism off with ``enabled: false`` (every admit
    becomes a pass, every record a no-op).
    """

    # Master switch. False = breakers are inert (always admit, never record).
    enabled: bool = True

    # Sliding-window look-back (seconds) for the failure-rate computation.
    window_seconds: float = Field(default=60.0, gt=0.0)

    # Minimum outcomes in the window before the rate threshold is even checked
    # (so a single early failure can't trip a breaker).
    min_samples: int = Field(default=5, ge=1)

    # Failure ratio (0.0–1.0) at/above which the breaker trips.
    error_rate_threshold: float = Field(default=0.5, ge=0.0, le=1.0)

    # Cool-down (seconds) after tripping before a half-open probe is admitted.
    open_seconds: float = Field(default=20.0, gt=0.0)

    # Max concurrent half-open recovery probes.
    half_open_max_probes: int = Field(default=1, ge=1)

    # Successful half-open probes required before model availability recovers.
    half_open_success_quorum: int = Field(default=1, ge=1)

    # Grace added to the owning attempt deadline for a recovery probe lease.
    probe_grace_seconds: float = Field(default=1.0, ge=0.0)

    @model_validator(mode="after")
    def validate_probe_quorum(self):
        if self.half_open_success_quorum > self.half_open_max_probes:
            raise ValueError("half_open_success_quorum cannot exceed half_open_max_probes")
        return self

    def to_breaker_config(self) -> BreakerConfig:
        """Project onto the framework-agnostic primitive config."""
        return BreakerConfig(
            window_seconds=self.window_seconds,
            min_samples=self.min_samples,
            error_rate_threshold=self.error_rate_threshold,
            open_seconds=self.open_seconds,
            half_open_max_probes=self.half_open_max_probes,
            half_open_success_quorum=self.half_open_success_quorum,
            probe_grace_seconds=self.probe_grace_seconds,
            enabled=self.enabled,
        )
