"""SpawnAdmissionPolicy core invariants and bounded extension authority."""

from __future__ import annotations

import asyncio

import pytest

from mote.contracts.policy.spawn import SpawnIntent, SpawnPolicyContribution
from mote.contracts.ports.spawn_policy import SpawnPolicyExtensionSpec
from mote.orchestration.environment.spawn_policy import build_spawn_admission_policy


def _intent(**changes) -> SpawnIntent:
    values = {
        "parent_path": "/root",
        "child_depth": 1,
        "max_depth": 2,
        "fleet_cost_usd": 0.0,
        "max_cost_usd": None,
        "fleet_total_tokens": 0,
        "max_total_tokens": None,
    }
    values.update(changes)
    return SpawnIntent(**values)


@pytest.mark.asyncio
async def test_core_depth_and_fleet_budgets_are_fail_closed():
    policy = build_spawn_admission_policy()

    depth = await policy.process(_intent(child_depth=3))
    cost = await policy.process(_intent(fleet_cost_usd=1.0, max_cost_usd=1.0))
    tokens = await policy.process(_intent(fleet_total_tokens=100, max_total_tokens=100))

    assert not depth.accepted and "depth limit" in depth.reason
    assert not cost.accepted and "cost budget" in cost.reason
    assert not tokens.accepted and "token budget" in tokens.reason


@pytest.mark.asyncio
async def test_extension_can_deny_or_narrow_but_cannot_expand_authority():
    class Narrow:
        async def evaluate(self, intent):
            return SpawnPolicyContribution(max_depth=1)

    class Expand:
        async def evaluate(self, intent):
            return SpawnPolicyContribution(max_depth=99)

    narrowed = build_spawn_admission_policy((SpawnPolicyExtensionSpec("organization", Narrow),))
    expanded = build_spawn_admission_policy((SpawnPolicyExtensionSpec("organization", Expand),))

    narrow_decision = await narrowed.process(_intent(child_depth=2))
    expand_decision = await expanded.process(_intent())

    assert not narrow_decision.accepted
    assert "depth limit (1)" in narrow_decision.reason
    assert not expand_decision.accepted
    assert "denied for safety" in expand_decision.reason


@pytest.mark.asyncio
async def test_extension_failure_and_timeout_deny_spawn():
    class Broken:
        async def evaluate(self, intent):
            raise RuntimeError("broken")

    class Slow:
        async def evaluate(self, intent):
            await asyncio.sleep(1)
            return SpawnPolicyContribution()

    broken = build_spawn_admission_policy((SpawnPolicyExtensionSpec("broken", Broken),))
    slow = build_spawn_admission_policy((SpawnPolicyExtensionSpec("slow", Slow, timeout=0.01),))

    assert not (await broken.process(_intent())).accepted
    assert not (await slow.process(_intent())).accepted


@pytest.mark.asyncio
async def test_extension_factory_is_per_policy_and_manifest_is_sealed():
    made: list[object] = []

    class Neutral:
        async def evaluate(self, intent):
            return SpawnPolicyContribution()

    def factory():
        extension = Neutral()
        made.append(extension)
        return extension

    spec = SpawnPolicyExtensionSpec("organization", factory)
    first = build_spawn_admission_policy((spec,))
    second = build_spawn_admission_policy((spec,))

    assert first.manifest == (spec,)
    assert second.manifest == (spec,)
    assert len(made) == 2 and made[0] is not made[1]

    with pytest.raises(ValueError, match="invalid"):
        build_spawn_admission_policy((spec, spec))
