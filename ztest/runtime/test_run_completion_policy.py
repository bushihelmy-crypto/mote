"""RunCompletionPolicy core gates and bounded extension authority."""

from __future__ import annotations

import asyncio

import pytest

from mote.contracts.output.policy import RunCompletionIntent, RunCompletionPolicyContribution
from mote.contracts.ports.output.run_completion_policy import RunCompletionPolicyExtensionSpec
from mote.runtime.agent.completion import build_run_completion_policy
from mote.runtime.hook.manager import HookManager


def _intent(**changes) -> RunCompletionIntent:
    values = {
        "output_committed": False,
        "background_pending": False,
        "remaining_continuations": 1,
    }
    values.update(changes)
    return RunCompletionIntent(**values)


@pytest.mark.asyncio
async def test_stop_hook_requests_bounded_continuation_with_context():
    manager = HookManager()
    manager.register(
        "Stop",
        lambda _input: {
            "decision": "block",
            "additionalContext": "keep working",
        },
    )

    decision = await build_run_completion_policy(hook_manager=manager).process(_intent())

    assert decision.continue_run
    assert decision.additional_context == ("keep working",)


@pytest.mark.asyncio
async def test_committed_output_and_budget_are_non_overridable_core_gates():
    class Request:
        async def evaluate(self, intent):
            return RunCompletionPolicyContribution(request_continuation=True)

    policy = build_run_completion_policy(extensions=(RunCompletionPolicyExtensionSpec("request", Request),))

    committed = await policy.process(_intent(output_committed=True))
    exhausted = await policy.process(_intent(remaining_continuations=0))

    assert not committed.continue_run
    assert committed.reason == "output committed"
    assert not exhausted.continue_run
    assert exhausted.reason == "continuation budget exhausted"


@pytest.mark.asyncio
async def test_pending_background_work_is_core_continuation_requirement():
    class Deny:
        async def evaluate(self, intent):
            return RunCompletionPolicyContribution(deny_continuation=True)

    policy = build_run_completion_policy(extensions=(RunCompletionPolicyExtensionSpec("deny", Deny),))

    decision = await policy.process(_intent(background_pending=True))

    assert decision.continue_run
    assert decision.reason == "background work pending"


@pytest.mark.asyncio
async def test_extension_can_request_or_deny_only_discretionary_continuation():
    class Request:
        async def evaluate(self, intent):
            return RunCompletionPolicyContribution(request_continuation=True)

    class Deny:
        async def evaluate(self, intent):
            return RunCompletionPolicyContribution(deny_continuation=True)

    policy = build_run_completion_policy(
        extensions=(
            RunCompletionPolicyExtensionSpec("request", Request),
            RunCompletionPolicyExtensionSpec("deny", Deny),
        )
    )

    decision = await policy.process(_intent())

    assert not decision.continue_run
    assert decision.reason == "continuation denied"


@pytest.mark.asyncio
async def test_extension_failure_and_timeout_never_create_extra_work():
    class Broken:
        async def evaluate(self, intent):
            raise RuntimeError("broken")

    class Slow:
        async def evaluate(self, intent):
            await asyncio.sleep(1)
            return RunCompletionPolicyContribution(request_continuation=True)

    policy = build_run_completion_policy(
        extensions=(
            RunCompletionPolicyExtensionSpec("broken", Broken),
            RunCompletionPolicyExtensionSpec("slow", Slow, timeout=0.01),
        )
    )

    decision = await policy.process(_intent())

    assert not decision.continue_run
    assert [entry.disposition for entry in decision.trace].count("failed_safe") == 2


def test_extension_factory_is_per_policy_and_manifest_is_sealed():
    made: list[object] = []

    class Neutral:
        async def evaluate(self, intent):
            return RunCompletionPolicyContribution()

    def factory():
        extension = Neutral()
        made.append(extension)
        return extension

    spec = RunCompletionPolicyExtensionSpec("organization", factory)
    first = build_run_completion_policy(extensions=(spec,))
    second = build_run_completion_policy(extensions=(spec,))

    assert first.manifest == (spec,)
    assert second.manifest == (spec,)
    assert len(made) == 2 and made[0] is not made[1]

    with pytest.raises(ValueError, match="invalid"):
        build_run_completion_policy(extensions=(spec, spec))
