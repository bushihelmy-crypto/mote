from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from mote.contracts.tool import ToolsetProtocolError
from mote.kernel.execution.run_context import RunContext
from mote.runtime.tools.base_tool import BaseTool
from mote.runtime.tools.definitions import native_definition, xml_definition
from mote.runtime.tools.dynamic_toolset import NativeDynamicToolset, XmlDynamicToolset
from mote.runtime.tools.provider import NativeToolset, XmlToolset
from mote.runtime.tools.tool_executor import ToolExecutor


class Echo(BaseTool):
    name = "Echo"

    async def call(self, *, text: str) -> str:
        """Return text.

        Args:
            text: Text to return.
        """

        return text


@dataclass
class DynamicDependencies:
    enabled: bool = True


class TrackingNativeToolset(NativeToolset[DynamicDependencies]):
    def __init__(
        self,
        label: str,
        events: list[str],
        *,
        fail_enter: bool = False,
    ) -> None:
        self._label = label
        self._events = events
        self._fail_enter = fail_enter
        super().__init__(label, (native_definition(Echo),))

    async def __aenter__(self) -> TrackingNativeToolset:
        self._events.append(f"enter:{self._label}")
        if self._fail_enter:
            raise RuntimeError(f"enter failed: {self._label}")
        return self

    async def __aexit__(self, *_exc: object) -> bool | None:
        self._events.append(f"exit:{self._label}")
        return None


@pytest.mark.asyncio
async def test_dynamic_native_toolset_is_run_scoped_and_composable() -> None:
    dynamic = NativeDynamicToolset[DynamicDependencies](
        "dynamic",
        lambda ctx: (NativeToolset("inner", (native_definition(Echo),)) if ctx.deps.enabled else None),
    ).prefix("tenant")
    executor = ToolExecutor(
        "session",
        tools=["tenant_Echo"],
        toolsets=(dynamic,),
        command_protocol="native",
    )
    ctx = RunContext(
        deps=DynamicDependencies(enabled=True),
        session_id="session",
        run_id="run",
    )

    assert executor.native_tool_specs() == []
    await executor.start_run(ctx)
    assert executor.native_tool_specs()[0]["name"] == "tenant_Echo"
    result = await executor.run_command("tenant_Echo", {"text": "ok"})
    assert result.output == "ok"
    await executor.end_run()
    assert executor.native_tool_specs() == []


@pytest.mark.asyncio
async def test_per_step_dynamic_toolset_refreshes_visibility_in_place() -> None:
    deps = DynamicDependencies(enabled=True)
    dynamic = NativeDynamicToolset[DynamicDependencies](
        "dynamic",
        lambda ctx: (NativeToolset("inner", (native_definition(Echo),)) if ctx.deps.enabled else None),
        per_run_step=True,
    )
    executor = ToolExecutor(
        "session",
        tools=["Echo"],
        toolsets=(dynamic,),
        command_protocol="native",
    )
    ctx = RunContext(deps=deps, session_id="session", run_id="run")

    await executor.start_run(ctx)
    assert executor.native_tool_specs() == []
    await executor.prepare_run_step(ctx)
    assert [spec["name"] for spec in executor.native_tool_specs()] == ["Echo"]

    deps.enabled = False
    await executor.prepare_run_step(ctx)
    assert executor.native_tool_specs() == []
    await executor.end_run()


@pytest.mark.asyncio
async def test_per_step_dynamic_instructions_follow_the_active_inner() -> None:
    deps = DynamicDependencies(enabled=True)
    dynamic = NativeDynamicToolset[DynamicDependencies](
        "dynamic",
        lambda ctx: NativeToolset(
            "inner",
            (native_definition(Echo),),
            instructions=("Enabled tools." if ctx.deps.enabled else "Disabled tools."),
        ),
        per_run_step=True,
    ).with_instructions("Static tenant policy.")
    executor = ToolExecutor(
        "session",
        tools=["Echo"],
        toolsets=(dynamic,),
        command_protocol="native",
    )
    ctx = RunContext(deps=deps, session_id="session", run_id="run")

    assert executor.static_toolset_instructions() == ("Static tenant policy.",)
    assert executor.dynamic_toolset_instructions() == ()
    await executor.start_run(ctx)
    await executor.prepare_run_step(ctx)
    assert executor.dynamic_toolset_instructions() == ("Enabled tools.",)

    deps.enabled = False
    await executor.prepare_run_step(ctx)
    assert executor.dynamic_toolset_instructions() == ("Disabled tools.",)
    await executor.end_run()


@pytest.mark.asyncio
async def test_dynamic_native_toolset_rejects_xml_factory_result() -> None:
    dynamic = NativeDynamicToolset[None](
        "dynamic",
        lambda _ctx: XmlToolset("xml", (xml_definition(Echo),)),  # type: ignore[arg-type,return-value]
    )
    executor = ToolExecutor(
        "session",
        tools=["Echo"],
        toolsets=(dynamic,),
        command_protocol="native",
    )

    with pytest.raises(ToolsetProtocolError, match="expected NativeToolset"):
        await executor.start_run(RunContext(deps=None, session_id="session", run_id="run"))


@pytest.mark.asyncio
async def test_dynamic_xml_toolset_uses_only_xml_definitions() -> None:
    dynamic = XmlDynamicToolset[None](
        "dynamic-xml",
        lambda _ctx: XmlToolset("inner-xml", (xml_definition(Echo),)),
    )
    executor = ToolExecutor(
        "session",
        tools=["Echo"],
        toolsets=(dynamic,),
        command_protocol="xml",
    )
    ctx = RunContext(deps=None, session_id="session", run_id="run")

    await executor.start_run(ctx)
    assert set(executor.all_xml_tool_schemas()) == {"Echo"}
    result = await executor.run_command("Echo", {"text": "xml"})
    assert result.output == "xml"
    with pytest.raises(TypeError, match="no Native"):
        executor.native_tool_specs()
    await executor.end_run()


@pytest.mark.asyncio
async def test_dynamic_inner_lifecycle_is_entered_and_exited_once() -> None:
    events: list[str] = []
    dynamic = NativeDynamicToolset[DynamicDependencies](
        "dynamic",
        lambda _ctx: TrackingNativeToolset("inner", events),
    )
    executor = ToolExecutor(
        "session",
        tools=["Echo"],
        toolsets=(dynamic,),
        command_protocol="native",
    )
    ctx = RunContext(
        deps=DynamicDependencies(),
        session_id="session",
        run_id="run",
    )

    await executor.start_run(ctx)
    assert events == ["enter:inner"]
    await executor.end_run()
    assert events == ["enter:inner", "exit:inner"]


@pytest.mark.asyncio
async def test_per_step_refresh_exits_previous_inner_before_entering_next() -> None:
    events: list[str] = []
    generation = 0

    def build(_ctx: RunContext[DynamicDependencies]) -> TrackingNativeToolset:
        nonlocal generation
        generation += 1
        return TrackingNativeToolset(f"inner-{generation}", events)

    dynamic = NativeDynamicToolset[DynamicDependencies](
        "dynamic",
        build,
        per_run_step=True,
    )
    executor = ToolExecutor(
        "session",
        tools=["Echo"],
        toolsets=(dynamic,),
        command_protocol="native",
    )
    ctx = RunContext(
        deps=DynamicDependencies(),
        session_id="session",
        run_id="run",
    )

    await executor.start_run(ctx)
    await executor.prepare_run_step(ctx)
    await executor.prepare_run_step(ctx)
    await executor.end_run()

    assert events == [
        "enter:inner-1",
        "exit:inner-1",
        "enter:inner-2",
        "exit:inner-2",
    ]


@pytest.mark.asyncio
async def test_failed_dynamic_enter_does_not_poison_executor_lifecycle() -> None:
    events: list[str] = []
    fail_enter = True

    def build(_ctx: RunContext[DynamicDependencies]) -> TrackingNativeToolset:
        return TrackingNativeToolset("inner", events, fail_enter=fail_enter)

    dynamic = NativeDynamicToolset[DynamicDependencies]("dynamic", build)
    executor = ToolExecutor(
        "session",
        tools=["Echo"],
        toolsets=(dynamic,),
        command_protocol="native",
    )
    ctx = RunContext(
        deps=DynamicDependencies(),
        session_id="session",
        run_id="run",
    )

    with pytest.raises(RuntimeError, match="enter failed"):
        await executor.start_run(ctx)

    fail_enter = False
    await executor.start_run(ctx)
    assert (await executor.run_command("Echo", {"text": "ok"})).output == "ok"
    await executor.end_run()
    assert events == ["enter:inner", "enter:inner", "exit:inner"]


@pytest.mark.asyncio
async def test_failed_per_step_factory_keeps_run_lifecycle_reusable() -> None:
    attempts = 0

    def build(_ctx: RunContext[DynamicDependencies]) -> NativeToolset[DynamicDependencies]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("factory failed")
        return NativeToolset("inner", (native_definition(Echo),))

    dynamic = NativeDynamicToolset[DynamicDependencies](
        "dynamic",
        build,
        per_run_step=True,
    )
    executor = ToolExecutor(
        "session",
        tools=["Echo"],
        toolsets=(dynamic,),
        command_protocol="native",
    )
    ctx = RunContext(
        deps=DynamicDependencies(),
        session_id="session",
        run_id="run",
    )

    await executor.start_run(ctx)
    with pytest.raises(RuntimeError, match="factory failed"):
        await executor.prepare_run_step(ctx)

    await executor.prepare_run_step(ctx)
    assert (await executor.run_command("Echo", {"text": "ok"})).output == "ok"
    await executor.end_run()


@pytest.mark.asyncio
async def test_dynamic_refresh_preserves_native_mcp_catalog() -> None:
    deps = DynamicDependencies(enabled=True)
    dynamic = NativeDynamicToolset[DynamicDependencies](
        "dynamic",
        lambda ctx: (NativeToolset("inner", (native_definition(Echo),)) if ctx.deps.enabled else None),
        per_run_step=True,
    )

    class Remote(Echo):
        name = "Remote"

    executor = ToolExecutor(
        "session",
        tools=["Echo"],
        toolsets=(dynamic,),
        command_protocol="native",
    )
    executor.register_native_tool(
        replace(native_definition(Remote), category="mcp"),
        Remote(),
    )
    ctx = RunContext(deps=deps, session_id="session", run_id="run")

    await executor.start_run(ctx)
    await executor.prepare_run_step(ctx)
    assert set(executor._catalog.mcp_names()) == {"Remote"}
    assert {spec["name"] for spec in executor.native_tool_specs()} == {"Echo", "Remote"}

    deps.enabled = False
    await executor.prepare_run_step(ctx)
    assert set(executor._catalog.mcp_names()) == {"Remote"}
    assert [spec["name"] for spec in executor.native_tool_specs()] == ["Remote"]
    await executor.end_run()
