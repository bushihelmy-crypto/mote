#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""End-to-end: PreToolUse / PostToolUse hooks inside ToolExecutor.run_command.

Confirms the hook layer composes with the permission engine at the single
dispatch chokepoint: a PreToolUse deny blocks the tool (deny-wins), updated_args
rewrites the call, and PostToolUse additionalContext is appended to the output.
With no hook_manager the executor behaves exactly as before.
"""
from __future__ import annotations

import pytest

from mote.common.events import EventBus
from mote.common.hook.manager import HookManager
from mote.common.hook.subscriber import HookSubscriber
from mote.common.interface.event_subscriber import ControlSubscriber, ObservationSubscriber
from mote.common.schema import PermissionConfig
from mote.executor.base_tool import BaseTool
from mote.executor.tool_executor import ToolExecutor

pytestmark = pytest.mark.asyncio


class SpyTool(BaseTool):
    name = "Spy"

    def __init__(self) -> None:
        super().__init__()
        self.ran = False
        self.seen_cmd = None

    def permission_target(self, args: dict) -> str:
        return args.get("cmd") or ""

    async def call(self, *, cmd: str = "") -> str:
        self.ran = True
        self.seen_cmd = cmd
        return f"ran:{cmd}"


def build(tool: BaseTool, *, hook_manager=None, config=None) -> ToolExecutor:
    bus = None
    if hook_manager is not None:
        bus = EventBus()
        bus.subscribe(HookSubscriber(hook_manager))
    ex = ToolExecutor("sess", tools=None, bus=bus, permission_config=config)
    tool.bind("sess")
    ex.register_tool_instance(tool, [tool.name, *getattr(tool, "aliases", [])])
    return ex


async def test_no_hook_manager_is_legacy():
    tool = SpyTool()
    ex = build(tool)
    res = await ex.run_command("Spy", {"cmd": "ls"})
    assert res.success and tool.ran and res.output == "ran:ls"


async def test_pre_tool_use_deny_blocks():
    tool = SpyTool()
    mgr = HookManager()
    mgr.register("PreToolUse", lambda hi: {"decision": "block", "reason": "no bash"})
    ex = build(tool, hook_manager=mgr)
    res = await ex.run_command("Spy", {"cmd": "ls"})
    assert res.success is False
    assert tool.ran is False
    assert 'code="TOOL_PERMISSION_DENIED"' in res.output
    assert "no bash" in res.output


async def test_pre_tool_use_updated_args_rewrites_call():
    tool = SpyTool()
    mgr = HookManager()
    mgr.register("PreToolUse", lambda hi: {"updatedInput": {"cmd": "safe"}})
    ex = build(tool, hook_manager=mgr)
    res = await ex.run_command("Spy", {"cmd": "danger"})
    assert res.success and tool.ran
    assert tool.seen_cmd == "safe"
    assert res.output == "ran:safe"


async def test_post_tool_use_appends_context():
    tool = SpyTool()
    mgr = HookManager()
    mgr.register("PostToolUse", lambda hi: {"additionalContext": "note: reviewed"})
    ex = build(tool, hook_manager=mgr)
    res = await ex.run_command("Spy", {"cmd": "ls"})
    assert res.success
    assert "ran:ls" in res.output
    assert "note: reviewed" in res.output


async def test_post_tool_use_output_rewrite_replaces_output():
    """A PostToolUse control subscriber returning ``updated_response`` replaces
    the tool's output text (truncate/redact channel), before any appended
    context."""
    from typing import Optional

    from mote.common.events import ToolResultOutcome
    from mote.common.events.types import POST_TOOL_USE, PostToolUseEvent
    from mote.common.interface.event_subscriber import ControlStage

    class Rewriter(ControlSubscriber):
        handles = (POST_TOOL_USE,)
        stage = ControlStage.REWRITE

        async def handle_control(self, event) -> Optional[ToolResultOutcome]:
            if isinstance(event, PostToolUseEvent):
                return ToolResultOutcome(updated_response="[redacted]", additional_context=["note"])
            return None

    tool = SpyTool()
    bus = EventBus()
    bus.subscribe(Rewriter())
    ex = ToolExecutor("sess", tools=None, bus=bus)
    tool.bind("sess")
    ex.register_tool_instance(tool, [tool.name])
    res = await ex.run_command("Spy", {"cmd": "ls"})
    assert res.success and tool.ran
    # base output replaced, then context appended on top
    assert res.output == "[redacted]\nnote"


async def test_post_tool_use_event_carries_structured_success():
    """PostToolUseEvent carries the ToolResult's ``success``/``error`` verbatim.

    An observer reads the executor's fact directly (P0) instead of sniffing the
    ``tool_response`` string: a successful call stamps ``success=True, error=None``;
    a tool that raises ToolError stamps ``success=False`` with a structured
    ``ErrorReport``. This is the seam the CLI ViewProjector reads to judge ``ok``.
    """
    from typing import Optional

    from mote.common.events.types import POST_TOOL_USE, PostToolUseEvent
    from mote.common.exception import ErrorReport
    from mote.common.interface.event_subscriber import ControlStage
    from mote.executor.tool_result import ToolError, ToolResult

    seen: list = []

    class Observer(ControlSubscriber):
        handles = (POST_TOOL_USE,)
        stage = ControlStage.REWRITE

        async def handle_control(self, event) -> Optional[object]:
            if isinstance(event, PostToolUseEvent):
                seen.append((event.success, event.error))
            return None

    class FailTool(BaseTool):
        """Returns a structured failure ToolResult (NOT a raise).

        A *raised* ToolError short-circuits before the PostToolUse emit (executor
        except-arm returns directly), so it never reaches this event — that gap is
        a separate concern from P0. A tool that *returns* ``success=False`` goes
        through the normal PostToolUse path, which is what P0 propagates.
        """

        name = "Fail"

        def permission_target(self, args: dict) -> str:
            return ""

        async def call(self, **_kw):
            report = ErrorReport.from_exception(ToolError("kaboom"))
            return ToolResult(output="boom", success=False, error=report)

    # Success path: the fact rides on the event as success=True, error=None.
    ok_tool = SpyTool()
    bus = EventBus()
    bus.subscribe(Observer())
    ex = ToolExecutor("sess", tools=None, bus=bus)
    ok_tool.bind("sess")
    ex.register_tool_instance(ok_tool, [ok_tool.name])
    await ex.run_command("Spy", {"cmd": "ls"})
    assert seen[-1][0] is True
    assert seen[-1][1] is None

    # Structured-failure return path: success=False + the ErrorReport ride along.
    fail = FailTool()
    fail.bind("sess")
    ex.register_tool_instance(fail, [fail.name])
    await ex.run_command("Fail", {})
    assert seen[-1][0] is False
    assert seen[-1][1] is not None


async def test_raised_tool_error_notifies_observers_of_failure():
    """A tool that *raises* still fans a failed PostToolUseEvent to observers.

    Tools signal failure by ``raise ToolError`` (the near-universal path), caught
    by run_command's except arm which builds a failed ToolResult and settles it.
    Because the tool *ran*, the settle emit goes on the control plane, but an
    observer still sees the event (emit = control + observe), so the front-end
    can render "failed + reason" instead of leaving a dangling ToolCallStarted.
    The model's <error> block (the return value) is unaffected — a separate path.
    """
    from mote.common.events.types import POST_TOOL_USE, PostToolUseEvent

    seen: list = []

    class Observer(ObservationSubscriber):
        priority = 50

        async def handle(self, event) -> None:
            if isinstance(event, PostToolUseEvent):
                seen.append((event.tool_name, event.success, event.error))

    class RaiseTool(BaseTool):
        name = "Raise"

        def permission_target(self, args: dict) -> str:
            return ""

        async def call(self, **_kw) -> str:
            from mote.executor.tool_result import ToolError

            raise ToolError("kaboom")

    tool = RaiseTool()
    bus = EventBus()
    bus.subscribe(Observer())
    ex = ToolExecutor("sess", tools=None, bus=bus)
    tool.bind("sess")
    ex.register_tool_instance(tool, [tool.name])
    res = await ex.run_command("Raise", {})

    # The returned result still carries the failure for the model.
    assert res.success is False
    # And the observer saw exactly one failed PostToolUseEvent for this call.
    assert seen == [("Raise", False, res.error)]
    assert seen[0][2] is not None


async def test_post_tool_use_block_marks_failure():
    tool = SpyTool()
    mgr = HookManager()
    mgr.register("PostToolUse", lambda hi: {"decision": "block", "reason": "bad output"})
    ex = build(tool, hook_manager=mgr)
    res = await ex.run_command("Spy", {"cmd": "ls"})
    assert res.success is False
    assert "bad output" in res.output


class AliasSpyTool(BaseTool):
    """A tool reachable by its canonical name ``Bash`` or the alias ``bash``."""

    name = "Bash"
    aliases = ["bash"]

    def __init__(self) -> None:
        super().__init__()
        self.ran = False

    def permission_target(self, args: dict) -> str:
        return ""

    async def call(self, **_kw) -> str:
        self.ran = True
        return "ran"


async def test_hook_matcher_fires_when_tool_invoked_by_alias():
    """A hook written against the canonical name fires even when the model
    invokes the tool via a snake_case alias — run_command canonicalizes the
    name before the PreToolUse event, so the matcher does not silently miss."""
    tool = AliasSpyTool()
    mgr = HookManager()
    mgr.register("PreToolUse", lambda hi: {"decision": "block", "reason": "no bash"}, matcher="Bash")
    ex = build(tool, hook_manager=mgr)
    # Invoke by the ALIAS; the matcher is keyed on the canonical name.
    res = await ex.run_command("bash", {})
    assert res.success is False
    assert tool.ran is False
    assert "no bash" in res.output


async def test_post_tool_use_event_reports_canonical_name_for_alias_call():
    """PostToolUse carries the canonical tool name even when invoked by alias."""
    tool = AliasSpyTool()
    control = _ControlRecorder()
    bus = _build_with(control)
    ex = ToolExecutor("sess", tools=None, bus=bus)
    tool.bind("sess")
    ex.register_tool_instance(tool, [tool.name, *tool.aliases])
    res = await ex.run_command("bash", {})
    assert res.success is True and tool.ran
    assert len(control.seen) == 1
    assert control.seen[0].tool_name == "Bash"


async def test_hook_deny_composes_with_permission_engine():
    # Permission engine would allow (allow rule), but the PreToolUse hook denies
    # -> deny wins.
    tool = SpyTool()
    mgr = HookManager()
    mgr.register("PreToolUse", lambda hi: {"decision": "block", "reason": "hook veto"})
    ex = build(tool, hook_manager=mgr, config=PermissionConfig(allow=["Spy"]))
    res = await ex.run_command("Spy", {"cmd": "ls"})
    assert res.success is False
    assert tool.ran is False


# ---------------------------------------------------------------------------
# P2 — the single settle join point: one PostToolUse per call, control-plane
# vs observation-only chosen by whether the tool actually ran.
# ---------------------------------------------------------------------------

from typing import Optional  # noqa: E402

from mote.common.events.types import POST_TOOL_USE, PostToolUseEvent  # noqa: E402
from mote.common.interface.event_subscriber import ControlStage  # noqa: E402
from mote.executor.tool_result import ToolError  # noqa: E402


class _RaiseTool(BaseTool):
    name = "Raise"

    def permission_target(self, args: dict) -> str:
        return ""

    async def call(self, **_kw) -> str:
        raise ToolError("kaboom")


class _ControlRecorder(ControlSubscriber):
    """A control-plane subscriber recording every PostToolUse it is asked to fold."""

    handles = (POST_TOOL_USE,)
    stage = ControlStage.REWRITE

    def __init__(self) -> None:
        self.seen: list = []

    async def handle_control(self, event) -> Optional[object]:
        if isinstance(event, PostToolUseEvent):
            self.seen.append(event)
        return None


class _Observer(ObservationSubscriber):
    """A pure observation-plane subscriber."""

    priority = 50

    def __init__(self) -> None:
        self.seen: list = []

    async def handle(self, event) -> None:
        if isinstance(event, PostToolUseEvent):
            self.seen.append(event)


def _build_with(*subs):
    bus = EventBus()
    for sub in subs:
        bus.subscribe(sub)
    return bus


async def test_raise_reaches_control_plane():
    """(a) A raised failure now traverses PostToolUse on the CONTROL plane —
    a hook / control subscriber fires on the tool error."""
    control = _ControlRecorder()
    bus = _build_with(control)
    ex = ToolExecutor("sess", tools=None, bus=bus)
    tool = _RaiseTool()
    tool.bind("sess")
    ex.register_tool_instance(tool, [tool.name])
    res = await ex.run_command("Raise", {})
    assert res.success is False
    assert len(control.seen) == 1
    assert control.seen[0].tool_name == "Raise"
    assert control.seen[0].success is False
    assert control.seen[0].error is not None


async def test_preflight_deny_is_observed_only():
    """(b) A pre-flight deny emits exactly one OBSERVED PostToolUse (front-end
    row closes) with success=False, and does NOT hit the control plane."""
    control = _ControlRecorder()
    observer = _Observer()
    bus = _build_with(control, observer)
    mgr = HookManager()
    mgr.register("PreToolUse", lambda hi: {"decision": "block", "reason": "no bash"})
    bus.subscribe(HookSubscriber(mgr))
    ex = ToolExecutor("sess", tools=None, bus=bus)
    tool = SpyTool()
    tool.bind("sess")
    ex.register_tool_instance(tool, [tool.name])
    res = await ex.run_command("Spy", {"cmd": "ls"})
    assert res.success is False and tool.ran is False
    # The observer saw exactly one lifecycle-end event for the denied call.
    assert len(observer.seen) == 1
    assert observer.seen[0].success is False
    # …but the deny never reached the PostToolUse control plane.
    assert control.seen == []


async def test_control_block_of_failed_result_is_harmless():
    """(c) A control subscriber blocking an already-failed result is a no-op —
    the result stays failed, nothing crashes."""
    from mote.common.events import ToolResultOutcome

    class Blocker(ControlSubscriber):
        handles = (POST_TOOL_USE,)
        stage = ControlStage.REWRITE

        async def handle_control(self, event) -> Optional[ToolResultOutcome]:
            if isinstance(event, PostToolUseEvent):
                return ToolResultOutcome(blocked=True, system_message="blocked anyway")
            return None

    bus = _build_with(Blocker())
    ex = ToolExecutor("sess", tools=None, bus=bus)
    tool = _RaiseTool()
    tool.bind("sess")
    ex.register_tool_instance(tool, [tool.name])
    res = await ex.run_command("Raise", {})
    assert res.success is False
    assert "blocked anyway" in res.output


async def test_bg_task_result_produces_post_tool_use_and_keeps_data():
    """(d) A BgTaskResult now emits a PostToolUse (row closes) and still carries
    the raw BgTaskResult in ``data``."""
    from mote.executor.tasks.types import BgTaskResult

    class BgTool(BaseTool):
        name = "Bg"

        async def call(self, *, label: str = "task") -> BgTaskResult:
            return BgTaskResult.foreground("started", command_name=label)

    control = _ControlRecorder()
    bus = _build_with(control)
    ex = ToolExecutor("sess", tools=None, bus=bus)
    tool = BgTool()
    tool.bind("sess")
    ex.register_tool_instance(tool, [tool.name])
    res = await ex.run_command("Bg", {"label": "crawl"})
    assert res.success is True
    assert isinstance(res.data, BgTaskResult)
    assert res.data.command_name == "crawl"
    assert len(control.seen) == 1
    assert control.seen[0].tool_name == "Bg"
    assert control.seen[0].success is True


async def test_exactly_one_post_tool_use_on_every_path():
    """(e) Every dispatch path settles with exactly one PostToolUse event
    (observed for not-ran, control+observed for ran)."""

    async def _count(run):
        observer = _Observer()
        bus = _build_with(observer)
        ex = ToolExecutor("sess", tools=None, bus=bus)
        await run(ex)
        return len(observer.seen)

    async def _unknown(ex):
        await ex.run_command("Nope", {})

    async def _raise(ex):
        t = _RaiseTool()
        t.bind("sess")
        ex.register_tool_instance(t, [t.name])
        await ex.run_command("Raise", {})

    async def _success(ex):
        t = SpyTool()
        t.bind("sess")
        ex.register_tool_instance(t, [t.name])
        await ex.run_command("Spy", {"cmd": "ls"})

    assert await _count(_unknown) == 1
    assert await _count(_raise) == 1
    assert await _count(_success) == 1


async def test_post_tool_use_hook_payload_carries_success_and_error():
    """The PostToolUse hook payload now carries the structured ``success``/``error``
    facts (no more sniffing the response text).

    Success stamps ``success=True, error=None``; a raised failure stamps
    ``success=False`` with the ``ErrorReport.as_dict()`` shape.
    """
    seen: list = []
    mgr = HookManager()
    mgr.register("PostToolUse", lambda hi: seen.append(hi.payload) or {})

    # Success path.
    ok = SpyTool()
    ex = build(ok, hook_manager=mgr)
    await ex.run_command("Spy", {"cmd": "ls"})
    assert seen[-1]["success"] is True
    assert seen[-1]["error"] is None

    # Raised-failure path (the tool ran → PostToolUse hook fires on the error).
    fail = _RaiseTool()
    ex2 = build(fail, hook_manager=mgr)
    await ex2.run_command("Raise", {})
    payload = seen[-1]
    assert payload["success"] is False
    assert isinstance(payload["error"], dict)
    assert payload["error"]["error"] == "ToolError"
    assert payload["error"]["message"] == "kaboom"
