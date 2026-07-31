"""Sealed child-Agent admission pipeline owned by orchestration."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from mote.contracts.agent.policy import SpawnDecision, SpawnIntent, SpawnPolicyContribution, SpawnPolicyTraceEntry
from mote.contracts.ports.agent.spawn_policy import SpawnPolicyExtensionSpec


class DefaultSpawnAdmissionPolicy:
    def __init__(
        self,
        extensions: tuple[SpawnPolicyExtensionSpec, ...] = (),
    ) -> None:
        identities: set[str] = set()
        installed: list[tuple[SpawnPolicyExtensionSpec, object]] = []
        for spec in extensions:
            if not spec.identity.strip() or spec.identity in identities:
                raise ValueError(f"invalid spawn policy extension: {spec.identity!r}")
            if spec.timeout <= 0:
                raise ValueError("spawn policy extension timeout must be positive")
            extension = spec.factory()
            if not callable(getattr(extension, "evaluate", None)):
                raise TypeError("spawn policy extension must expose evaluate()")
            identities.add(spec.identity)
            installed.append((spec, extension))
        self._manifest = tuple(extensions)
        self._extensions = tuple(installed)

    @property
    def manifest(self) -> tuple[SpawnPolicyExtensionSpec, ...]:
        return self._manifest

    async def process(self, intent: SpawnIntent) -> SpawnDecision:
        trace: list[SpawnPolicyTraceEntry] = []
        denied = self._core_denial(intent, trace)
        if denied:
            return SpawnDecision(False, denied, tuple(trace))

        effective = intent
        for spec, extension in self._extensions:
            step = f"extension:{spec.identity}"
            try:
                contribution = await asyncio.wait_for(
                    extension.evaluate(effective),  # type: ignore[attr-defined]
                    timeout=spec.timeout,
                )
                if not isinstance(contribution, SpawnPolicyContribution):
                    raise TypeError("invalid spawn policy contribution")
                effective = self._apply_narrowing(effective, contribution)
            except Exception as exc:  # noqa: BLE001 -- admission extensions fail closed
                trace.append(SpawnPolicyTraceEntry(step, "failed_closed", type(exc).__name__))
                return SpawnDecision(
                    False,
                    f"spawn policy extension '{spec.identity}' failed; denied for safety.",
                    tuple(trace),
                )
            if not contribution.allowed:
                trace.append(SpawnPolicyTraceEntry(step, "deny"))
                return SpawnDecision(
                    False,
                    contribution.reason or f"spawn denied by extension '{spec.identity}'",
                    tuple(trace),
                )
            trace.append(SpawnPolicyTraceEntry(step, "narrow"))
            denied = self._core_denial(effective, trace)
            if denied:
                return SpawnDecision(False, denied, tuple(trace))

        trace.append(SpawnPolicyTraceEntry("final_admission", "allow"))
        return SpawnDecision(True, trace=tuple(trace))

    @staticmethod
    def _core_denial(
        intent: SpawnIntent,
        trace: list[SpawnPolicyTraceEntry],
    ) -> str:
        if intent.max_depth is not None and intent.child_depth > intent.max_depth:
            trace.append(SpawnPolicyTraceEntry("framework_depth_limit", "deny"))
            return f"spawn depth limit ({intent.max_depth}) reached at " f"{intent.parent_path}"
        if intent.max_cost_usd is not None and intent.fleet_cost_usd >= intent.max_cost_usd:
            trace.append(SpawnPolicyTraceEntry("fleet_cost_budget", "deny"))
            return f"fleet cost budget (${intent.max_cost_usd:.2f}) reached " f"(${intent.fleet_cost_usd:.2f} spent)"
        if intent.max_total_tokens is not None and intent.fleet_total_tokens >= intent.max_total_tokens:
            trace.append(SpawnPolicyTraceEntry("fleet_token_budget", "deny"))
            return f"fleet token budget ({intent.max_total_tokens}) reached " f"({intent.fleet_total_tokens} used)"
        return ""

    @classmethod
    def _apply_narrowing(
        cls,
        intent: SpawnIntent,
        contribution: SpawnPolicyContribution,
    ) -> SpawnIntent:
        return replace(
            intent,
            max_depth=cls._narrow(intent.max_depth, contribution.max_depth),
            max_cost_usd=cls._narrow(intent.max_cost_usd, contribution.max_cost_usd),
            max_total_tokens=cls._narrow(intent.max_total_tokens, contribution.max_total_tokens),
        )

    @staticmethod
    def _narrow(current, proposed):
        if proposed is None:
            return current
        if proposed < 0 or (current is not None and proposed > current):
            raise ValueError("spawn policy extension attempted to expand authority")
        return proposed


def build_spawn_admission_policy(
    extensions: tuple[SpawnPolicyExtensionSpec, ...] = (),
) -> DefaultSpawnAdmissionPolicy:
    return DefaultSpawnAdmissionPolicy(extensions)


__all__ = ["DefaultSpawnAdmissionPolicy", "build_spawn_admission_policy"]
