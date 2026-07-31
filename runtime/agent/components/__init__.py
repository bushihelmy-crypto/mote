"""Domain-owned component manifests composed by the Role runtime."""

from mote.runtime.agent.components.action import (
    action_component_specs,
    build_args_limiter,
    dedupe_tools,
    effective_deferred_tools,
)
from mote.runtime.agent.components.cognition import cognition_component_specs
from mote.runtime.agent.components.context import context_component_specs
from mote.runtime.agent.components.integrations import (
    hook_available,
    integration_component_specs,
    integration_event_subscribers,
)
from mote.runtime.agent.components.policy import policy_component_specs
from mote.runtime.agent.components.session import session_component_specs, session_event_subscribers
from mote.runtime.agent.components.watching import WatchingCallbacks, watching_component_specs

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
    "policy_component_specs",
    "session_component_specs",
    "session_event_subscribers",
    "WatchingCallbacks",
    "watching_component_specs",
]
