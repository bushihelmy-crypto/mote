"""Sealed compaction policy with monotonic, capability-limited extensions."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from mote.contracts.conversation.compaction_policy import (
    CompactionDecision,
    CompactionIntent,
    CompactionPolicyContribution,
    CompactionPolicyTraceEntry,
)
from mote.contracts.ports.conversation.compaction_policy import CompactionPolicyExtension, CompactionPolicyExtensionSpec
from mote.runtime.hook.manager import HookManager

DEFAULT_COMPACTION_POLICY_TIMEOUT = 120.0
_PROFILE_RANK = {"preserve": 0, "balanced": 1, "emergency": 2}


@dataclass(frozen=True)
class _InstalledExtension:
    spec: CompactionPolicyExtensionSpec
    extension: CompactionPolicyExtension


class DefaultCompactionPolicy:
    def __init__(
        self,
        *,
        hook_manager: HookManager | None = None,
        extensions: tuple[CompactionPolicyExtensionSpec, ...] = (),
    ) -> None:
        identities: set[str] = set()
        installed: list[_InstalledExtension] = []
        for spec in extensions:
            if not spec.identity.strip() or spec.identity in identities:
                raise ValueError(f"invalid compaction policy extension: {spec.identity!r}")
            if spec.timeout <= 0:
                raise ValueError("compaction policy extension timeout must be positive")
            extension = spec.factory()
            if not callable(getattr(extension, "evaluate", None)):
                raise TypeError("compaction policy extension must expose evaluate()")
            identities.add(spec.identity)
            installed.append(_InstalledExtension(spec, extension))
        self._hook_manager = hook_manager
        self._manifest = tuple(extensions)
        self._extensions = tuple(installed)

    @property
    def manifest(self) -> tuple[CompactionPolicyExtensionSpec, ...]:
        return self._manifest

    async def process(self, intent: CompactionIntent) -> CompactionDecision:
        trace: list[CompactionPolicyTraceEntry] = []
        instructions = [intent.custom_instructions] if intent.custom_instructions else []
        profile = "emergency" if intent.urgency == "hard" else "balanced"

        manager = self._hook_manager
        if manager is not None:
            try:
                outcome = await asyncio.wait_for(
                    manager.fire("PreCompact", {"trigger": intent.trigger}),
                    timeout=DEFAULT_COMPACTION_POLICY_TIMEOUT,
                )
                instructions.extend(str(item) for item in outcome.additional_context)
                if outcome.additional_context:
                    trace.append(CompactionPolicyTraceEntry("pre_compact_hook", "enrich"))
                if outcome.stop or outcome.behavior == "deny":
                    trace.append(
                        CompactionPolicyTraceEntry(
                            "pre_compact_hook",
                            "ignored_veto",
                            "hook cannot disable core compaction",
                        )
                    )
            except Exception as exc:  # noqa: BLE001 -- advisory adapter fails open
                trace.append(CompactionPolicyTraceEntry("pre_compact_hook", "failed_open", type(exc).__name__))

        for installed in self._extensions:
            step = f"extension:{installed.spec.identity}"
            try:
                contribution = await asyncio.wait_for(
                    installed.extension.evaluate(intent),
                    timeout=installed.spec.timeout,
                )
                if not isinstance(contribution, CompactionPolicyContribution):
                    raise TypeError("invalid compaction policy contribution")
                if contribution.profile not in (None, "balanced", "preserve"):
                    raise ValueError("invalid compaction policy profile")
                if not all(isinstance(item, str) for item in contribution.additional_instructions):
                    raise TypeError("compaction instructions must be strings")
                instructions.extend(contribution.additional_instructions)
                if contribution.profile is not None and _PROFILE_RANK[contribution.profile] < _PROFILE_RANK[profile]:
                    profile = contribution.profile
                trace.append(CompactionPolicyTraceEntry(step, "narrow"))
            except Exception as exc:  # noqa: BLE001 -- degrade to safest profile
                profile = "preserve"
                trace.append(CompactionPolicyTraceEntry(step, "failed_closed", type(exc).__name__))

        allow_destructive = intent.urgency == "hard" and profile == "emergency"
        trace.append(CompactionPolicyTraceEntry("preservation_invariant", "allow"))
        return CompactionDecision(
            profile=profile,
            custom_instructions="\n".join(instructions),
            allow_destructive=allow_destructive,
            trace=tuple(trace),
        )


def build_compaction_policy(
    *,
    hook_manager: HookManager | None = None,
    extensions: tuple[CompactionPolicyExtensionSpec, ...] = (),
) -> DefaultCompactionPolicy:
    return DefaultCompactionPolicy(
        hook_manager=hook_manager,
        extensions=extensions,
    )


__all__ = [
    "DEFAULT_COMPACTION_POLICY_TIMEOUT",
    "DefaultCompactionPolicy",
    "build_compaction_policy",
]
