"""Core domain-policy component manifest."""

from __future__ import annotations

from mote.runtime.agent.component_graph import ComponentSpec
from mote.runtime.completion import build_run_completion_policy
from mote.runtime.prompt import build_prompt_policy


def policy_component_specs() -> list[ComponentSpec]:
    return [
        ComponentSpec("prompt_policy", _build_prompt_policy),
        ComponentSpec("run_completion_policy", _build_run_completion_policy),
    ]


def _build_prompt_policy(ctx):
    return build_prompt_policy(
        hook_manager=ctx.dep("hook_manager"),
        secret_store=ctx.dep("secret_store"),
        extensions=ctx.role.wiring.dependencies.prompt_policy_extensions,
    )


def _build_run_completion_policy(ctx):
    return build_run_completion_policy(
        hook_manager=ctx.dep("hook_manager"),
        extensions=ctx.role.wiring.dependencies.run_completion_policy_extensions,
    )


__all__ = ["policy_component_specs"]
