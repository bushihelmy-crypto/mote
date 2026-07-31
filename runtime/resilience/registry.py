"""ResourceHealthRegistry — one :class:`CircuitBreaker` per resource key.

A breaker guards ONE resource, but a resource (an LLM ``api_type::model::key_index``,
an MCP server, an egress host) is shared by many short-lived callers in one
Runtime Engine. Every ``BaseLLM`` instance built from the same Context consults
the same lazily-created per-key breaker.

The registry is deliberately thin: it owns the default :class:`BreakerConfig` and
a single ``on_transition`` hook (so all state changes flow to Telemetry through one
seam), and hands both to each breaker it creates. Domain knowledge — how a key is
spelled, what trips it — stays with the caller.

Each Runtime ``Context`` owns one registry and shares it with every model client
created by that Engine. Separate Engines therefore cannot contaminate each
other's breaker state.
"""

from __future__ import annotations

from typing import Dict, Optional

from mote.contracts.config.model.breaker import BreakerConfig

from .breaker import CircuitBreaker, TransitionHook


class ResourceHealthRegistry:
    """Lazily creates and caches one :class:`CircuitBreaker` per opaque key."""

    def __init__(
        self,
        config: Optional[BreakerConfig] = None,
        *,
        on_transition: Optional[TransitionHook] = None,
    ) -> None:
        self._config = config or BreakerConfig()
        self._on_transition = on_transition
        self._breakers: Dict[str, CircuitBreaker] = {}

    def breaker(self, key: str) -> CircuitBreaker:
        """Return the breaker for *key*, creating it on first use."""
        b = self._breakers.get(key)
        if b is None:
            b = CircuitBreaker(self._config, key=key, on_transition=self._on_transition)
            self._breakers[key] = b
        return b

    def admit(self, key: str) -> bool:
        """Whether a call to *key* should proceed (see :meth:`CircuitBreaker.admit`)."""
        return self.breaker(key).admit()

    def record(self, key: str, success: bool) -> None:
        """Record the outcome of an admitted call to *key*."""
        self.breaker(key).record(success)

    def snapshot(self) -> Dict[str, str]:
        """Current state per known key — for introspection / diagnostics."""
        return {key: b.state.value for key, b in self._breakers.items()}

    def set_transition_hook(self, hook: Optional[TransitionHook]) -> None:
        """Set the observer for FUTURE breakers (existing ones keep their hook).

        Wired once at app start (before any resource is touched) so every breaker
        created thereafter mirrors transitions onto Telemetry through one seam.
        """
        self._on_transition = hook

    def set_config(self, config: BreakerConfig) -> None:
        """Set the :class:`BreakerConfig` for FUTURE breakers (existing keep theirs).

        Symmetric with :meth:`set_transition_hook`: applied at app start from the
        config-v2 ``ResilienceConfig`` (before any resource is touched), so every
        breaker created thereafter inherits the configured thresholds. Mutates the
        existing registry in place so every holder in the Engine sees the new config.
        """
        self._config = config


__all__ = ["ResourceHealthRegistry"]
