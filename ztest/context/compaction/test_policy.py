"""CompactionPolicy invariants and bounded extension authority."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from mote.contracts.conversation.compaction_policy import CompactionIntent, CompactionPolicyContribution
from mote.contracts.ports.conversation.compaction_policy import CompactionPolicyExtensionSpec
from mote.runtime.context.compaction.policy import build_compaction_policy
from mote.runtime.hook.manager import HookManager


def _intent(*, urgency: str = "soft") -> CompactionIntent:
    return CompactionIntent(
        trigger="auto",
        target_tokens=100,
        urgency=urgency,
        custom_instructions="caller",
    )


@pytest.mark.asyncio
async def test_hook_enriches_but_cannot_veto_core_compaction():
    manager = HookManager()
    manager.register(
        "PreCompact",
        lambda _input: {
            "continue": False,
            "additionalContext": "organization focus",
        },
    )

    decision = await build_compaction_policy(hook_manager=manager).process(_intent())

    assert decision.profile == "balanced"
    assert decision.custom_instructions == "caller\norganization focus"
    assert not decision.allow_destructive
    assert any(entry.disposition == "ignored_veto" for entry in decision.trace)


@pytest.mark.asyncio
async def test_extension_profiles_only_narrow_core_authority():
    class Balanced:
        async def evaluate(self, intent):
            return CompactionPolicyContribution(profile="balanced")

    class Preserve:
        async def evaluate(self, intent):
            return CompactionPolicyContribution(profile="preserve")

    balanced = build_compaction_policy(extensions=(CompactionPolicyExtensionSpec("balanced", Balanced),))
    preserve = build_compaction_policy(
        extensions=(
            CompactionPolicyExtensionSpec("preserve", Preserve),
            CompactionPolicyExtensionSpec("balanced", Balanced),
        )
    )

    balanced_decision = await balanced.process(_intent(urgency="hard"))
    preserve_decision = await preserve.process(_intent(urgency="hard"))

    assert balanced_decision.profile == "balanced"
    assert not balanced_decision.allow_destructive
    assert preserve_decision.profile == "preserve"
    assert not preserve_decision.allow_destructive


@pytest.mark.asyncio
async def test_only_unrestricted_hard_core_policy_allows_destructive_reduction():
    soft = await build_compaction_policy().process(_intent())
    hard = await build_compaction_policy().process(_intent(urgency="hard"))

    assert soft.profile == "balanced" and not soft.allow_destructive
    assert hard.profile == "emergency" and hard.allow_destructive


@pytest.mark.asyncio
async def test_extension_failure_timeout_and_invalid_profile_degrade_to_preserve():
    class Broken:
        async def evaluate(self, intent):
            raise RuntimeError("broken")

    class Slow:
        async def evaluate(self, intent):
            await asyncio.sleep(1)
            return CompactionPolicyContribution()

    class Invalid:
        async def evaluate(self, intent):
            return CompactionPolicyContribution(profile=cast(Any, "emergency"))

    specs = (
        CompactionPolicyExtensionSpec("broken", Broken),
        CompactionPolicyExtensionSpec("slow", Slow, timeout=0.01),
        CompactionPolicyExtensionSpec("invalid", Invalid),
    )
    decision = await build_compaction_policy(extensions=specs).process(_intent(urgency="hard"))

    assert decision.profile == "preserve"
    assert not decision.allow_destructive
    assert [entry.disposition for entry in decision.trace].count("failed_closed") == 3


def test_extension_factory_is_per_policy_and_manifest_is_sealed():
    made: list[object] = []

    class Neutral:
        async def evaluate(self, intent):
            return CompactionPolicyContribution()

    def factory():
        extension = Neutral()
        made.append(extension)
        return extension

    spec = CompactionPolicyExtensionSpec("organization", factory)
    first = build_compaction_policy(extensions=(spec,))
    second = build_compaction_policy(extensions=(spec,))

    assert first.manifest == (spec,)
    assert second.manifest == (spec,)
    assert len(made) == 2 and made[0] is not made[1]

    with pytest.raises(ValueError, match="invalid"):
        build_compaction_policy(extensions=(spec, spec))
