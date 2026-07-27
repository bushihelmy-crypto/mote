"""ToolCallPolicy's typed deny-only extension slot."""
from __future__ import annotations

import asyncio

import pytest

from mote.contracts.permissions import PermissionFacts
from mote.contracts.policy.tool import ToolCallInspection, ToolCallIntent
from mote.contracts.ports.tool_policy import ToolCallPolicyExtensionSpec
from mote.runtime.tools.permission import ToolCallInspector
from mote.runtime.tools.policy import DefaultToolCallPolicy

pytestmark = pytest.mark.asyncio


class _Allowlist(ToolCallInspector):
    def __init__(self, allowed: set[str]) -> None:
        self._allowed = allowed
        self.seen: list[tuple[ToolCallIntent, PermissionFacts]] = []

    async def inspect(
        self,
        intent: ToolCallIntent,
        facts: PermissionFacts,
    ) -> ToolCallInspection:
        self.seen.append((intent, facts))
        if intent.tool_name in self._allowed:
            return ToolCallInspection.allow()
        return ToolCallInspection.deny(f"{intent.tool_name} is not on the allowlist")


class _Boom(ToolCallInspector):
    async def inspect(
        self,
        intent: ToolCallIntent,
        facts: PermissionFacts,
    ) -> ToolCallInspection:
        raise RuntimeError("gate exploded")


class _Slow(ToolCallInspector):
    async def inspect(
        self,
        intent: ToolCallIntent,
        facts: PermissionFacts,
    ) -> ToolCallInspection:
        await asyncio.sleep(1)
        return ToolCallInspection.allow()


def _facts(args: dict) -> PermissionFacts:
    return PermissionFacts(targets=[args.get("path", "")])


async def test_allowlist_receives_typed_intent_and_facts():
    extension = _Allowlist({"Read"})
    policy = DefaultToolCallPolicy(extensions=(ToolCallPolicyExtensionSpec("allowlist", lambda: extension),))

    decision = await policy.authorize(
        ToolCallIntent("Read", {"path": "/workspace/a"}),
        _facts,
    )

    assert decision.allowed
    assert extension.seen[0][0].arguments == {"path": "/workspace/a"}
    assert extension.seen[0][1].targets == ["/workspace/a"]


async def test_extension_can_deny_but_cannot_force_core_allow():
    extension = _Allowlist({"Read"})
    policy = DefaultToolCallPolicy(extensions=(ToolCallPolicyExtensionSpec("allowlist", lambda: extension),))

    decision = await policy.authorize(
        ToolCallIntent("Bash", {"path": "/workspace/a"}),
        _facts,
    )

    assert not decision.allowed
    assert decision.reason == "Bash is not on the allowlist"
    assert decision.trace[-1].step == "extension:allowlist"


@pytest.mark.parametrize(
    ("extension", "timeout", "detail"),
    [
        (_Boom(), 5.0, "RuntimeError"),
        (_Slow(), 0.001, "timeout"),
    ],
)
async def test_extension_failure_is_fail_closed(extension, timeout, detail):
    policy = DefaultToolCallPolicy(
        extensions=(
            ToolCallPolicyExtensionSpec(
                "security-gate",
                lambda: extension,
                timeout,
            ),
        )
    )

    decision = await policy.authorize(ToolCallIntent("Read"), _facts)

    assert not decision.allowed
    assert decision.trace[-1].disposition == "failed_closed"
    assert decision.trace[-1].detail == detail


async def test_manifest_is_sealed_and_rejects_ambiguous_identity():
    extension = _Allowlist({"Read"})
    specs = [ToolCallPolicyExtensionSpec("allowlist", lambda: extension)]
    policy = DefaultToolCallPolicy(extensions=tuple(specs))
    specs.clear()
    assert len(policy.manifest) == 1

    with pytest.raises(ValueError, match="duplicate"):
        DefaultToolCallPolicy(
            extensions=(
                ToolCallPolicyExtensionSpec("same", lambda: extension),
                ToolCallPolicyExtensionSpec("same", lambda: extension),
            )
        )


async def test_incomplete_extension_cannot_be_instantiated():
    class Incomplete(ToolCallInspector):
        pass

    with pytest.raises(TypeError):
        Incomplete()
