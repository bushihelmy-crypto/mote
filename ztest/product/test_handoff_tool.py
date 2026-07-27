from __future__ import annotations

import pytest

from mote.contracts.handoff import HandoffOutcome, HandoffStatus
from mote.contracts.runtimes import RuntimeRef
from mote.contracts.tools.effects import ToolEffect
from mote.product.toolsets.builtin.canvas import Canvas
from mote.product.toolsets.builtin.device_use import DeviceUse
from mote.product.toolsets.builtin.python import Python
from mote.product.toolsets.builtin.terminal import Terminal
from mote.product.toolsets.builtin.web_browser import WebBrowser
from mote.runtime.interactive import RuntimeHost
from mote.runtime.tools.dependency._canvas import CanvasRuntimeDriver


@pytest.mark.parametrize(
    ("tool_type", "runtime"),
    [
        (Terminal, "terminal:default"),
        (Python, "jupyter:default"),
        (WebBrowser, "browser:default"),
        (DeviceUse, "device:default"),
        (Canvas, "canvas:default"),
    ],
    ids=lambda value: getattr(value, "name", value),
)
@pytest.mark.asyncio
async def test_stateful_tool_handoff_action_returns_structured_human_outcome(tool_type, runtime):
    expected = HandoffOutcome(
        status=HandoffStatus.COMPLETED,
        runtime_ref=RuntimeRef(runtime_id="r-1", kind=runtime.split(":", 1)[0]),
        from_revision=3,
        to_revision=4,
        human_message="I aligned the labels",
        summary="runtime returned",
    )
    tool = tool_type()

    if tool_type is Canvas:
        host = RuntimeHost()
        await host.create(CanvasRuntimeDriver(), runtime_id="canvas-default")
        tool.get_runtime_host = lambda: host

    async def handoff_runtime(runtime_name: str, *, message: str = "") -> HandoffOutcome:
        assert runtime_name == runtime
        assert message == "Please adjust the labels"
        return expected

    tool.handoff_runtime = handoff_runtime
    result = await tool.call(action="handoff", message="Please adjust the labels")

    assert result.success is True
    if tool_type is Canvas:
        assert result.data.elements == []
    else:
        assert result.data is expected
    assert "I aligned the labels" in result.output
    assert tool.check_permissions({"action": " HANDOFF "}).behavior == "allow"
    if tool_type is Canvas:
        await host.close("canvas:default")


def test_canvas_handoff_is_external_without_reclassifying_canvas_updates():
    tool = Canvas()

    assert tool.resolve_effect_for({"action": "handoff"}) is ToolEffect.EXTERNAL
    assert tool.resolve_effect_for({"operations": []}) is ToolEffect.LOCAL
