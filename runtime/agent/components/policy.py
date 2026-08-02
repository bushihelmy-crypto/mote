"""Core domain-policy component manifest."""

from __future__ import annotations

from dataclasses import dataclass

from mote.contracts.ports.conversation.prompt_policy import PromptPolicyExtensionSpec
from mote.contracts.ports.output.run_completion_policy import RunCompletionPolicyExtensionSpec
from mote.runtime.agent.completion import build_run_completion_policy
from mote.runtime.agent.component_graph import ComponentSpec
from mote.runtime.agent.component_keys import HOOK_MANAGER, PROMPT_POLICY, RUN_COMPLETION_POLICY, SECRET_STORE
from mote.runtime.prompt import build_prompt_policy


@dataclass(frozen=True, slots=True)
class PolicyComponentInputs:
    prompt_extensions: tuple[PromptPolicyExtensionSpec, ...] = ()
    completion_extensions: tuple[RunCompletionPolicyExtensionSpec, ...] = ()


def policy_component_specs(inputs: PolicyComponentInputs = PolicyComponentInputs()) -> list[ComponentSpec]:
    return [
        ComponentSpec(PROMPT_POLICY, lambda ctx: _build_prompt_policy(ctx, inputs)),
        ComponentSpec(RUN_COMPLETION_POLICY, lambda ctx: _build_run_completion_policy(ctx, inputs)),
    ]


def _build_prompt_policy(ctx, inputs: PolicyComponentInputs):
    return build_prompt_policy(
        hook_manager=ctx.dep(HOOK_MANAGER),
        secret_store=ctx.dep(SECRET_STORE),
        extensions=inputs.prompt_extensions,
    )


def _build_run_completion_policy(ctx, inputs: PolicyComponentInputs):
    return build_run_completion_policy(
        hook_manager=ctx.dep(HOOK_MANAGER),
        extensions=inputs.completion_extensions,
    )


__all__ = ["PolicyComponentInputs", "policy_component_specs"]
