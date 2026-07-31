"""Core domain-policy component manifest."""

from __future__ import annotations

from mote.runtime.agent.completion import build_run_completion_policy
from mote.runtime.agent.component_graph import ComponentSpec
from mote.runtime.agent.component_keys import HOOK_MANAGER, PROMPT_POLICY, RUN_COMPLETION_POLICY, SECRET_STORE
from mote.runtime.prompt import build_prompt_policy


def policy_component_specs() -> list[ComponentSpec]:
    return [
        ComponentSpec(PROMPT_POLICY, _build_prompt_policy),
        ComponentSpec(RUN_COMPLETION_POLICY, _build_run_completion_policy),
    ]


def _build_prompt_policy(ctx):
    return build_prompt_policy(
        hook_manager=ctx.dep(HOOK_MANAGER),
        secret_store=ctx.dep(SECRET_STORE),
        extensions=ctx.role.wiring.dependencies.prompt_policy_extensions,
    )


def _build_run_completion_policy(ctx):
    return build_run_completion_policy(
        hook_manager=ctx.dep(HOOK_MANAGER),
        extensions=ctx.role.wiring.dependencies.run_completion_policy_extensions,
    )


__all__ = ["policy_component_specs"]
