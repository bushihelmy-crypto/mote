"""ResourceHealthRegistry — one :class:`CircuitBreaker` per resource key.

A breaker guards ONE resource, but a resource (an LLM ``api_type::model::key_index``,
an MCP server, an egress host) is shared by many short-lived callers — every
``BaseLLM`` instance targeting the same model must consult the *same* breaker, or
each would independently re-learn the outage. So breakers live in a process-global
registry, lazily created per key and shared thereafter.

The registry is deliberately thin: it owns the default :class:`BreakerConfig` and
a single ``on_transition`` hook (so all state changes flow to the bus through one
seam), and hands both to each breaker it creates. Domain knowledge — how a key is
spelled, what trips it — stays with the caller.

A module-level default instance (:func:`get_health_registry`) is the shared
process singleton the LLM layer uses; :func:`reset_health_registry` clears it for
test isolation. Callers wanting an isolated registry just construct their own.
"""

from __future__ import annotations

from typing import Dict, Optional

from .breaker import CircuitBreaker, TransitionHook
from .config import BreakerConfig


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
        created thereafter mirrors transitions onto the bus through one seam.
        """
        self._on_transition = hook

    def set_config(self, config: BreakerConfig) -> None:
        """Set the :class:`BreakerConfig` for FUTURE breakers (existing keep theirs).

        Symmetric with :meth:`set_transition_hook`: applied at app start from the
        config-v2 ``ResilienceConfig`` (before any resource is touched), so every
        breaker created thereafter inherits the configured thresholds. Mutates the
        existing singleton in place rather than replacing it, so any early holder of
        the registry reference sees the new config.
        """
        self._config = config


_default_registry: Optional[ResourceHealthRegistry] = None


def get_health_registry() -> ResourceHealthRegistry:
    """The shared process-global registry (created on first call)."""
    global _default_registry
    if _default_registry is None:
        _default_registry = ResourceHealthRegistry()
    return _default_registry


def reset_health_registry() -> None:
    """Drop the process-global registry (test isolation)."""
    global _default_registry
    _default_registry = None


def configure_health_registry(config: BreakerConfig) -> None:
    """Apply the configured :class:`BreakerConfig` to the shared registry.

    Called once at app start from the config-v2 ``ResilienceConfig`` validator,
    before any resource is touched, so every breaker inherits the configured
    thresholds. Idempotent and safe to call before the LLM layer wires the bus
    hook (:meth:`ResourceHealthRegistry.set_config` only affects future breakers).
    """
    get_health_registry().set_config(config)


__all__ = [
    "ResourceHealthRegistry",
    "get_health_registry",
    "reset_health_registry",
    "configure_health_registry",
]
