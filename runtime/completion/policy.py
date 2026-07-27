"""Sealed post-flow completion policy with bounded continuation extensions."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from mote.contracts.policy.run_completion import (
    RunCompletionDecision,
    RunCompletionIntent,
    RunCompletionPolicyContribution,
    RunCompletionPolicyTraceEntry,
)
from mote.contracts.ports.run_completion_policy import RunCompletionPolicyExtension, RunCompletionPolicyExtensionSpec
from mote.runtime.hook.manager import HookManager

DEFAULT_RUN_COMPLETION_POLICY_TIMEOUT = 120.0


@dataclass(frozen=True)
class _InstalledExtension:
    spec: RunCompletionPolicyExtensionSpec
    extension: RunCompletionPolicyExtension


class DefaultRunCompletionPolicy:
    def __init__(
        self,
        *,
        hook_manager: HookManager | None = None,
        extensions: tuple[RunCompletionPolicyExtensionSpec, ...] = (),
    ) -> None:
        identities: set[str] = set()
        installed: list[_InstalledExtension] = []
        for spec in extensions:
            if not spec.identity.strip() or spec.identity in identities:
                raise ValueError(f"invalid run completion policy extension: {spec.identity!r}")
            if spec.timeout <= 0:
                raise ValueError("run completion policy extension timeout must be positive")
            extension = spec.factory()
            if not callable(getattr(extension, "evaluate", None)):
                raise TypeError("run completion policy extension must expose evaluate()")
            identities.add(spec.identity)
            installed.append(_InstalledExtension(spec, extension))
        self._hook_manager = hook_manager
        self._manifest = tuple(extensions)
        self._extensions = tuple(installed)

    @property
    def manifest(self) -> tuple[RunCompletionPolicyExtensionSpec, ...]:
        return self._manifest

    async def process(self, intent: RunCompletionIntent) -> RunCompletionDecision:
        trace: list[RunCompletionPolicyTraceEntry] = []
        additional_context: list[str] = []
        requested = False
        denied = False

        manager = self._hook_manager
        if manager is not None:
            try:
                outcome = await asyncio.wait_for(
                    manager.fire("Stop", {}),
                    timeout=DEFAULT_RUN_COMPLETION_POLICY_TIMEOUT,
                )
                if outcome.behavior == "deny":
                    requested = True
                    trace.append(RunCompletionPolicyTraceEntry("stop_hook", "request_continue"))
                additional_context.extend(str(item) for item in outcome.additional_context)
                if outcome.system_message:
                    additional_context.append(outcome.system_message)
            except Exception as exc:  # noqa: BLE001 -- optional adapter fails safe
                trace.append(RunCompletionPolicyTraceEntry("stop_hook", "failed_safe", type(exc).__name__))

        for installed in self._extensions:
            step = f"extension:{installed.spec.identity}"
            try:
                contribution = await asyncio.wait_for(
                    installed.extension.evaluate(intent),
                    timeout=installed.spec.timeout,
                )
                if not isinstance(contribution, RunCompletionPolicyContribution):
                    raise TypeError("invalid run completion policy contribution")
                if not all(isinstance(item, str) for item in contribution.additional_context):
                    raise TypeError("run completion context must be strings")
                additional_context.extend(contribution.additional_context)
                requested = requested or contribution.request_continuation
                denied = denied or contribution.deny_continuation
                trace.append(RunCompletionPolicyTraceEntry(step, "bounded"))
            except Exception as exc:  # noqa: BLE001 -- no accidental extra work
                denied = True
                trace.append(RunCompletionPolicyTraceEntry(step, "failed_safe", type(exc).__name__))

        if intent.output_committed:
            continue_run = False
            reason = "output committed"
            disposition = "complete"
        elif intent.remaining_continuations <= 0:
            continue_run = False
            reason = "continuation budget exhausted"
            disposition = "budget_stop"
        elif intent.background_pending:
            continue_run = True
            reason = "background work pending"
            disposition = "core_continue"
        elif denied:
            continue_run = False
            reason = "continuation denied"
            disposition = "deny"
        elif requested:
            continue_run = True
            reason = "bounded continuation requested"
            disposition = "continue"
        else:
            continue_run = False
            reason = "turn settled"
            disposition = "complete"

        trace.append(RunCompletionPolicyTraceEntry("core_completion_gate", disposition))
        return RunCompletionDecision(
            continue_run=continue_run,
            additional_context=tuple(additional_context) if continue_run else (),
            reason=reason,
            trace=tuple(trace),
        )


def build_run_completion_policy(
    *,
    hook_manager: HookManager | None = None,
    extensions: tuple[RunCompletionPolicyExtensionSpec, ...] = (),
) -> DefaultRunCompletionPolicy:
    return DefaultRunCompletionPolicy(
        hook_manager=hook_manager,
        extensions=extensions,
    )


__all__ = [
    "DEFAULT_RUN_COMPLETION_POLICY_TIMEOUT",
    "DefaultRunCompletionPolicy",
    "build_run_completion_policy",
]
