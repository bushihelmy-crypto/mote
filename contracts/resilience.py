"""BreakerConfig — the runtime knobs of a single :class:`CircuitBreaker`.

A plain frozen dataclass on purpose: this is the *primitive* layer's config, a
leaf with no pydantic / config-v2 dependency so the state machine stays domain-
and framework-agnostic (any subsystem — LLM, MCP, egress — can construct a
breaker without dragging in the app config graph). The pydantic ``ResilienceConfig``
in the config-v2 surface maps *onto* this; it does not replace it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BreakerConfig:
    """Failure-detection + cool-down parameters for one resource's breaker.

    Defaults are deliberately conservative so a breaker only trips on *sustained*
    failure (``min_samples`` real outcomes at/over ``error_rate_threshold`` within
    ``window_seconds``), and recovers quickly (a short ``open_seconds`` cool-down
    then a single half-open probe). ``enabled=False`` makes every ``admit`` a pass
    and every ``record`` a no-op — the breaker becomes inert.
    """

    #: Sliding-window look-back for failure-rate computation.
    window_seconds: float = 60.0
    #: Minimum outcomes in the window before the rate threshold is even checked.
    min_samples: int = 5
    #: Failure ratio (0.0–1.0) at/above which the breaker trips.
    error_rate_threshold: float = 0.5
    #: Cool-down after tripping before a half-open probe is admitted.
    open_seconds: float = 20.0
    #: Max concurrent half-open recovery probes (clamped to >= 1 at use).
    half_open_max_probes: int = 1
    #: Successful half-open probes required before model availability recovers.
    half_open_success_quorum: int = 1
    #: Grace added to an attempt deadline when leasing a model recovery probe.
    probe_grace_seconds: float = 1.0
    #: Master switch. When False the breaker is inert (always admits, never records).
    enabled: bool = True
