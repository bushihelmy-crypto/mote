"""Domain-owned component manifests composed by the Role runtime."""

from mote.roles.runtime_modules.action import (
    action_component_specs,
    build_args_limiter,
    dedupe_tools,
    effective_deferred_tools,
)
from mote.roles.runtime_modules.cognition import cognition_component_specs
from mote.roles.runtime_modules.context import context_component_specs
from mote.roles.runtime_modules.integrations import (
    hook_available,
    integration_component_specs,
    integration_event_subscribers,
)
from mote.roles.runtime_modules.session import session_component_specs, session_event_subscribers
from mote.roles.runtime_modules.watching import WatchingCallbacks, watching_component_specs

__all__ = [
    "action_component_specs",
    "context_component_specs",
    "cognition_component_specs",
    "build_args_limiter",
    "dedupe_tools",
    "effective_deferred_tools",
    "hook_available",
    "integration_component_specs",
    "integration_event_subscribers",
    "session_component_specs",
    "session_event_subscribers",
    "WatchingCallbacks",
    "watching_component_specs",
]
