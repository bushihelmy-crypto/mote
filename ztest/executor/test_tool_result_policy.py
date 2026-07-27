"""Security and semantics of the ToolResult presentation pipeline."""
from __future__ import annotations

import pytest

from mote.contracts.policy.tool import ToolResultIntent
from mote.contracts.schema import LoopGuardConfig
from mote.runtime.hook.manager import HookManager
from mote.runtime.tools.policy import build_tool_result_policy

pytestmark = pytest.mark.asyncio

SECRET = "credential-super-secret-value"
LABEL = "<agent-vault:credential>"


class _Store:
    def as_map(self):
        return {SECRET: LABEL}


class _BrokenStore:
    def as_map(self):
        raise RuntimeError("vault unavailable")


async def test_hook_receives_only_redacted_output_arguments_and_error():
    seen: list[dict] = []
    manager = HookManager()
    manager.register(
        "PostToolUse",
        lambda hook_input: seen.append(hook_input.payload) or {},
    )
    policy = build_tool_result_policy(
        hook_manager=manager,
        secret_store=_Store(),
    )

    presentation = await policy.present(
        ToolResultIntent(
            tool_name="Probe",
            arguments={"token": SECRET},
            output=f"output={SECRET}",
            execution_success=False,
            error={"message": f"failed with {SECRET}"},
        )
    )

    assert SECRET not in repr(seen)
    assert seen[0]["tool_input"]["token"] == LABEL
    assert seen[0]["tool_response"] == f"output={LABEL}"
    assert seen[0]["error"]["message"] == f"failed with {LABEL}"
    assert SECRET not in presentation.output


async def test_hook_cannot_reintroduce_known_secret():
    manager = HookManager()
    manager.register(
        "PostToolUse",
        lambda _input: {"updatedResponse": f"rewritten={SECRET}"},
    )
    policy = build_tool_result_policy(
        hook_manager=manager,
        secret_store=_Store(),
    )

    presentation = await policy.present(ToolResultIntent(tool_name="Probe"))

    assert presentation.output == f"rewritten={LABEL}"


async def test_redaction_failure_withholds_output_fail_closed():
    policy = build_tool_result_policy(secret_store=_BrokenStore())

    presentation = await policy.present(ToolResultIntent(tool_name="Probe", output=SECRET))

    assert SECRET not in presentation.output
    assert "withheld" in presentation.output
    assert any(entry.disposition == "failed_closed" for entry in presentation.trace)


async def test_rejected_call_skips_post_execution_hook():
    calls: list[str] = []
    manager = HookManager()
    manager.register(
        "PostToolUse",
        lambda _input: calls.append("called") or {},
    )
    policy = build_tool_result_policy(hook_manager=manager)

    await policy.present(
        ToolResultIntent(
            tool_name="Probe",
            output="denied",
            execution_success=False,
            executed=False,
        )
    )

    assert calls == []


async def test_loop_guard_enriches_safe_representation_without_changing_truth():
    policy = build_tool_result_policy(
        loop_guard_config=LoopGuardConfig(
            enabled=True,
            failure_threshold=2,
            no_progress_threshold=2,
        )
    )
    intent = ToolResultIntent(
        tool_name="Read",
        arguments={"path": "/same"},
        output="same content",
        execution_success=True,
        is_readonly=True,
    )

    first = await policy.present(intent)
    second = await policy.present(intent)

    assert "loop guard" not in first.output
    assert "loop guard" in second.output
