"""Direct contract tests for the sealed ToolCall authorization pipeline."""
from __future__ import annotations

import asyncio

import pytest

from mote.contracts.authorization import PermissionDecision, PermissionFacts
from mote.contracts.tool.policy import ToolCallIntent
from mote.runtime.hook.manager import HookManager
from mote.runtime.tools.permission.engine import PermissionEngine
from mote.runtime.tools.permission.rule_store import RuleStore
from mote.runtime.tools.policy import DefaultToolCallPolicy

pytestmark = pytest.mark.asyncio


def _facts(args: dict) -> PermissionFacts:
    return PermissionFacts(targets=[args.get("cmd", "")])


class _RecordingEngine:
    def __init__(self, decision: PermissionDecision | None = None) -> None:
        self.targets: list[str] = []
        self._decision = decision or PermissionDecision.allow("test")

    async def check(self, tool_name: str, *, target: str, **kwargs):
        self.targets.append(target)
        return self._decision


class _BoomEngine:
    async def check(self, tool_name: str, **kwargs):
        raise RuntimeError("permission backend unavailable")


class _SlowEngine:
    async def check(self, tool_name: str, **kwargs):
        await asyncio.sleep(1)
        return PermissionDecision.allow("test")


class _RewritingEngine:
    def __init__(self) -> None:
        self.targets: list[str] = []

    async def check(self, tool_name: str, *, target: str, **kwargs):
        self.targets.append(target)
        return PermissionDecision.allow(
            "test",
            updated_args={"cmd": "safe-final-target"},
        )


class _AskEngine:
    async def check(self, tool_name: str, **kwargs):
        return PermissionDecision.ask("broken", "unresolved ask")


async def test_hook_rewrite_is_reclassified_before_permission():
    hook = HookManager()
    hook.register(
        "PreToolUse",
        lambda _input: {"updatedInput": {"cmd": "safe-final-target"}},
    )
    engine = _RecordingEngine()
    policy = DefaultToolCallPolicy(
        hook_manager=hook,
        permission_engine=engine,
    )

    decision = await policy.authorize(
        ToolCallIntent("Bash", {"cmd": "dangerous-original-target"}),
        _facts,
    )

    assert decision.allowed
    assert decision.arguments == {"cmd": "safe-final-target"}
    assert engine.targets == ["safe-final-target"]


async def test_hook_deny_short_circuits_permission():
    hook = HookManager()
    hook.register(
        "PreToolUse",
        lambda _input: {"decision": "block", "reason": "organization policy"},
    )
    engine = _RecordingEngine()
    policy = DefaultToolCallPolicy(
        hook_manager=hook,
        permission_engine=engine,
    )

    decision = await policy.authorize(ToolCallIntent("Bash"), _facts)

    assert not decision.allowed
    assert decision.reason == "organization policy"
    assert engine.targets == []


@pytest.mark.parametrize(
    ("engine", "timeout", "detail"),
    [
        (_BoomEngine(), 5.0, "RuntimeError"),
        (_SlowEngine(), 0.001, "timeout"),
    ],
)
async def test_permission_failure_is_fail_closed(engine, timeout, detail):
    policy = DefaultToolCallPolicy(
        permission_engine=engine,
        timeout=timeout,
    )

    decision = await policy.authorize(ToolCallIntent("Bash"), _facts)

    assert not decision.allowed
    assert decision.trace[-1].disposition == "failed_closed"
    assert decision.trace[-1].detail == detail


async def test_permission_rewrite_is_reclassified_before_final_allow():
    engine = _RewritingEngine()
    policy = DefaultToolCallPolicy(permission_engine=engine)

    decision = await policy.authorize(
        ToolCallIntent("Bash", {"cmd": "original-target"}),
        _facts,
    )

    assert decision.allowed
    assert decision.arguments == {"cmd": "safe-final-target"}
    assert engine.targets == ["original-target", "safe-final-target"]


async def test_non_terminal_permission_decision_is_fail_closed():
    policy = DefaultToolCallPolicy(permission_engine=_AskEngine())

    decision = await policy.authorize(ToolCallIntent("Bash"), _facts)

    assert not decision.allowed
    assert decision.trace[-1].disposition == "failed_closed"
    assert decision.trace[-1].detail == "ValueError"


async def test_user_rejection_is_terminal():
    async def deny(_request):
        return "deny"

    engine = PermissionEngine(
        mode="default",
        store=RuleStore(),
        ask_user=deny,
    )
    policy = DefaultToolCallPolicy(permission_engine=engine)

    decision = await policy.authorize(
        ToolCallIntent("Bash", {"cmd": "deploy"}),
        _facts,
    )

    assert not decision.allowed
    assert decision.terminate


async def test_trace_never_records_rewritten_argument_values():
    secret = "credential-super-secret-value"
    hook = HookManager()
    hook.register(
        "PreToolUse",
        lambda _input: {"updatedInput": {"token": secret, "cmd": "safe"}},
    )
    policy = DefaultToolCallPolicy(hook_manager=hook)

    decision = await policy.authorize(
        ToolCallIntent("Deploy", {"token": "original", "cmd": "unsafe"}),
        _facts,
    )

    assert decision.allowed
    assert secret not in repr(decision.trace)
    assert decision.trace[0].rewritten_fields == ("cmd", "token")
