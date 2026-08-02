"""Domain-owned component manifests composed by the Role runtime."""

from mote.runtime.agent.components.action import (
    action_component_specs,
    build_args_limiter,
    dedupe_tools,
    effective_deferred_tools,
)
from mote.runtime.agent.components.cognition import CognitionComponentInputs, cognition_component_specs
from mote.runtime.agent.components.context import ContextComponentInputs, context_component_specs
from mote.runtime.agent.components.integrations import (
    IntegrationComponentInputs,
    hook_available,
    integration_component_specs,
    integration_event_subscribers,
)
from mote.runtime.agent.components.policy import PolicyComponentInputs, policy_component_specs
from mote.runtime.agent.components.session import (
    SessionComponentInputs,
    event_fabric_component_spec,
    session_component_specs,
    session_event_subscribers,
)
from mote.runtime.agent.components.watching import WatchingCallbacks, WatchingComponentInputs, watching_component_specs

__all__ = [
    "action_component_specs",
    "context_component_specs",
    "ContextComponentInputs",
    "cognition_component_specs",
    "CognitionComponentInputs",
    "build_args_limiter",
    "dedupe_tools",
    "effective_deferred_tools",
    "event_fabric_component_spec",
    "hook_available",
    "integration_component_specs",
    "IntegrationComponentInputs",
    "integration_event_subscribers",
    "policy_component_specs",
    "PolicyComponentInputs",
    "session_component_specs",
    "SessionComponentInputs",
    "session_event_subscribers",
    "WatchingCallbacks",
    "WatchingComponentInputs",
    "watching_component_specs",
]
