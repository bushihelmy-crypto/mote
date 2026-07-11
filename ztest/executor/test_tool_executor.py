#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for ``mote.executor.tool_executor.ToolExecutor``.

The dispatch tests inject already-bound instances via ``register_tool_instance``
(see ``make_executor``) so they never touch the global registry. One test
exercises the constructor's registry-prebind path using the
``restore_global_registry`` snapshot fixture.
"""
from __future__ import annotations

import pytest
from mote.common.exception import ToolValidationError
from mote.common.interface.event_subscriber import ObservationSubscriber
from mote.common.schema import ToolResultLimitConfig
from mote.executor.base_tool import BaseTool
from mote.executor.mcp_adapter import MCPToolAdapter
from mote.executor.tasks.types import BgTaskResult
from mote.executor.tool_executor import ToolExecutor, _validate_call_args

from .conftest import (
    AddTool,
    BgTool,
    BoomTool,
    EchoTool,
    FailTool,
    FakeRole,
    MediaTool,
    StructuredResultTool,
    make_executor,
)

pytestmark = pytest.mark.asyncio


class TestRunCommandDispatch:
    async def test_dispatch_by_primary_name(self):
        ex = make_executor(EchoTool())
        result = await ex.run_command("Echo", {"text": "hi"})
        assert result.success is True
        assert result.output == "hi"

    async def test_dispatch_by_alias(self):
        ex = make_executor(EchoTool())
        result = await ex.run_command("echo", {"text": "yo"})
        assert result.output == "yo"

    async def test_kwargs_default_to_empty(self):
        ex = make_executor(AddTool())
        # `b` defaults to 0 — passing only `a`.
        result = await ex.run_command("Add", {"a": 5})
        assert result.output == "5"

    async def test_unknown_tool_returns_failure(self):
        ex = make_executor(EchoTool())
        result = await ex.run_command("Nope", {})
        assert result.success is False
        assert "unknown tool" in result.output
        # Available tools are listed for the model.
        assert "Echo" in result.output
        # Routed through the shared error contract: a uniform <error> block and
        # a machine-readable report on the result (no longer a bare string).
        assert 'code="TOOL_NOT_FOUND"' in result.output
        assert result.error is not None and result.error.code == "TOOL_NOT_FOUND"


class TestRunCommandErrors:
    async def test_tool_error_becomes_failure_result(self):
        ex = make_executor(FailTool())
        result = await ex.run_command("Fail", {"message": "missing file"})
        assert result.success is False
        # Rendered as the uniform <error> block; the typed report is carried too.
        assert result.output.startswith("<error ")
        assert "missing file" in result.output
        assert result.error is not None
        assert result.error.error == "ToolError"
        assert result.error.message == "missing file"

    async def test_generic_exception_becomes_failure_result(self):
        ex = make_executor(BoomTool())
        result = await ex.run_command("Boom", {})
        assert result.success is False
        assert result.output.startswith("<error ")
        # Un-typed exception degrades to an UNKNOWN report but is still surfaced.
        assert "kaboom" in result.output
        assert result.error is not None
        assert result.error.error == "RuntimeError"
        assert result.error.code == "UNKNOWN"


class TestRunCommandArgValidation:
    """LLM-supplied args are validated against the tool's call() signature
    before dispatch (a missing required / unexpected arg becomes a structured
    failure instead of an opaque TypeError)."""

    async def test_missing_required_arg_is_validation_failure(self):
        ex = make_executor(EchoTool())
        result = await ex.run_command("Echo", {})  # `text` is required
        assert result.success is False
        assert "Echo" in result.output
        assert "missing required argument: text" in result.output

    async def test_unexpected_arg_is_validation_failure(self):
        ex = make_executor(EchoTool())
        result = await ex.run_command("Echo", {"text": "hi", "bogus": 1})
        assert result.success is False
        assert "unexpected argument: bogus" in result.output

    async def test_optional_arg_omitted_still_succeeds(self):
        # Regression: AddTool's `b` has a default — omitting it must NOT trip
        # the required-arg check.
        ex = make_executor(AddTool())
        result = await ex.run_command("Add", {"a": 5})
        assert result.success is True
        assert result.output == "5"

    async def test_kwargs_tool_skips_validation(self):
        # A tool whose call() takes **kwargs has no statically-known params, so
        # validation is skipped and the call goes through untouched.
        class KwargsTool(BaseTool):
            name = "Kw"

            async def call(self, **kwargs) -> str:
                return ",".join(sorted(kwargs))

        ex = make_executor(KwargsTool())
        result = await ex.run_command("Kw", {"anything": 1, "goes": 2})
        assert result.success is True
        assert result.output == "anything,goes"

    async def test_validate_call_args_raises_tool_validation_error(self):
        # Unit-level: the helper raises a typed ToolValidationError.
        with pytest.raises(ToolValidationError) as exc:
            _validate_call_args(EchoTool.call, "Echo", {})
        assert "missing required argument: text" in str(exc.value)


class TestRunCommandReturnNormalization:
    async def test_structured_toolresult_passthrough(self):
        ex = make_executor(StructuredResultTool())
        result = await ex.run_command("Struct", {"ok": True})
        assert result.success is True
        assert result.output == "structured"
        assert result.data == {"k": "v"}

    async def test_bg_task_result_wrapped_in_data(self):
        ex = make_executor(BgTool())
        result = await ex.run_command("Bg", {"label": "crawl"})
        assert result.success is True
        assert result.output == "started"
        assert isinstance(result.data, BgTaskResult)
        assert result.data.command_name == "crawl"

    async def test_media_result_passthrough(self):
        ex = make_executor(MediaTool())
        result = await ex.run_command("Media", {"payload": "BASE64"})
        assert result.images == ["BASE64"]
        assert result.output == "Read image (1KB)"


class TestResultLimiting:
    async def test_large_output_persisted(self, tmp_path):
        big = "x" * 60_000  # over DEFAULT_MAX_RESULT_SIZE_CHARS (50k)

        from mote.executor.base_tool import BaseTool

        class BigTool(BaseTool):
            name = "Big"

            async def call(self):
                return big

        ex = make_executor(BigTool(), session_id="limit-sess")
        # Point persistence at a tmp dir via the module default base_dir.
        import mote.executor.tool_result_limit as trl

        orig = trl.DEFAULT_WORKSPACE_ROOT
        trl.DEFAULT_WORKSPACE_ROOT = tmp_path
        try:
            result = await ex.run_command("Big", {}, result_id="rid-1")
        finally:
            trl.DEFAULT_WORKSPACE_ROOT = orig
        assert result.output.startswith("<persisted-output>")
        assert (tmp_path / ".tool_results" / "limit-sess" / "rid-1.txt").exists()

    async def test_limiting_disabled_passes_through(self):
        big = "y" * 60_000

        from mote.executor.base_tool import BaseTool

        class BigTool2(BaseTool):
            name = "Big2"

            async def call(self):
                return big

        ex = make_executor(
            BigTool2(),
            limit_config=ToolResultLimitConfig(enable_tool_result_limit=False),
        )
        result = await ex.run_command("Big2", {})
        assert result.output == big

    async def test_media_result_not_limited(self):
        # Even oversized, media results bypass persistence (sent verbatim).
        from mote.executor.base_tool import BaseTool

        class BigMedia(BaseTool):
            name = "BigMedia"

            async def call(self):
                from mote.executor.tool_result import ToolResult

                return ToolResult(output="z" * 60_000, images=["img"])

        ex = make_executor(BigMedia())
        result = await ex.run_command("BigMedia", {})
        assert len(result.output) == 60_000
        assert not result.output.startswith("<persisted-output>")


class TestSchemas:
    async def test_get_tool_schemas_deduplicates_aliases(self):
        ex = make_executor(EchoTool())
        schemas = ex.get_tool_schemas()
        # Echo has aliases but appears once keyed by primary name.
        assert set(schemas) == {"Echo"}

    async def test_get_all_tool_schemas_includes_multiple_tools(self):
        ex = make_executor(EchoTool(), AddTool())
        schemas = ex.get_all_tool_schemas()
        assert set(schemas) == {"Echo", "Add"}

    async def test_native_tool_specs_anthropic(self):
        ex = make_executor(AddTool())
        specs = ex.get_native_tool_specs(provider="anthropic")
        assert len(specs) == 1
        spec = specs[0]
        assert spec["name"] == "Add"
        assert spec["input_schema"]["type"] == "object"

    async def test_native_tool_specs_openai(self):
        ex = make_executor(AddTool())
        specs = ex.get_native_tool_specs(provider="openai")
        assert specs[0]["type"] == "function"
        assert specs[0]["function"]["name"] == "Add"


class TestReconstructableNames:
    async def test_default_tools_are_not_reconstructable(self):
        # EchoTool/AddTool don't opt in, so the derived set is empty.
        ex = make_executor(EchoTool(), AddTool())
        assert ex.reconstructable_tool_names() == frozenset()

    async def test_opted_in_tool_and_all_aliases_included(self):
        class ReconTool(EchoTool):
            name = "Recon"
            aliases = ["recon", "Recon.run"]
            reconstructable = True

        ex = make_executor(ReconTool(), AddTool())
        names = ex.reconstructable_tool_names()
        # Every name the tool routes under is present; the non-opted tool is not.
        assert names == frozenset({"Recon", "recon", "Recon.run"})
        assert "Add" not in names


class TestMcpFiltering:
    def _adapter(self, name="server:tool"):
        schema = {
            "name": name,
            "description": "an mcp tool",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        }
        return MCPToolAdapter(mcp=None, tool_name=name, schema=schema)

    async def test_builtin_schemas_exclude_mcp(self):
        ex = make_executor(EchoTool())
        ex.register_tool_instance(self._adapter(), ["server:tool"])
        builtin = ex.get_tool_schemas()
        assert "Echo" in builtin
        assert "server:tool" not in builtin

    async def test_mcp_schemas_only(self):
        ex = make_executor(EchoTool())
        ex.register_tool_instance(self._adapter(), ["server:tool"])
        mcp = ex.get_mcp_tool_schemas()
        assert set(mcp) == {"server:tool"}

    async def test_all_schemas_include_both(self):
        ex = make_executor(EchoTool())
        ex.register_tool_instance(self._adapter(), ["server:tool"])
        assert set(ex.get_all_tool_schemas()) == {"Echo", "server:tool"}


class TestPipelineFiltering:
    """A pipeline tool is recognised by holding a compiled-graph executor —
    an instance attribute stamped by ``mark_pipeline_executor`` — so it lands
    in its own category, separate from built-in and MCP tools."""

    def _pipeline_tool(self, name="Pipe"):
        from mote.executor.tasks.bggraph.marker import mark_pipeline_executor

        async def _exec(**state):  # a stand-in compiled-graph executor
            return None

        class PipelineTool(BaseTool):
            async def call(self, **kwargs):
                return None

        PipelineTool.name = name
        tool = PipelineTool()
        # Wiring a compiled executor onto the instance is what makes it a pipeline.
        tool._executor = mark_pipeline_executor(_exec)
        return tool

    async def test_builtin_schemas_exclude_pipeline(self):
        ex = make_executor(EchoTool(), self._pipeline_tool())
        builtin = ex.get_tool_schemas()
        assert "Echo" in builtin
        assert "Pipe" not in builtin

    async def test_pipeline_schemas_only(self):
        ex = make_executor(EchoTool(), self._pipeline_tool())
        pipeline = ex.get_pipeline_tool_schemas()
        assert set(pipeline) == {"Pipe"}

    async def test_pipeline_excluded_from_mcp(self):
        ex = make_executor(self._pipeline_tool())
        assert ex.get_mcp_tool_schemas() == {}

    async def test_all_schemas_include_pipeline(self):
        ex = make_executor(EchoTool(), self._pipeline_tool())
        assert set(ex.get_all_tool_schemas()) == {"Echo", "Pipe"}


class TestConstructorAndCleanup:
    async def test_constructor_prebinds_from_registry(self, restore_global_registry):
        # Register a test tool into the (snapshotted) global registry, then let
        # the constructor resolve it by name.
        from mote.executor.base_tool import BaseTool

        class RegTool(BaseTool):
            name = "RegTool"
            aliases = ["rt"]

            async def call(self, *, v: str = "v") -> str:
                return v

        restore_global_registry.register(RegTool)
        ex = ToolExecutor("sess", tools=["RegTool"])
        result = await ex.run_command("RegTool", {"v": "hello"})
        assert result.output == "hello"
        # Alias also routes to the same instance.
        assert await ex.run_command("rt", {"v": "z"}) is not None

    async def test_unknown_declared_tool_is_skipped(self, restore_global_registry):
        # A declared name with no registered class is silently skipped.
        ex = ToolExecutor("sess", tools=["DoesNotExist"])
        result = await ex.run_command("DoesNotExist", {})
        assert result.success is False

    async def test_cleanup_clears_tools(self):
        ex = make_executor(EchoTool())
        await ex.cleanup()
        result = await ex.run_command("Echo", {"text": "x"})
        assert result.success is False  # tool no longer registered

    async def test_role_capability_binding_through_executor(self):
        from .conftest import CapTool

        role = FakeRole({"greet": lambda: "bound!"})
        ex = make_executor(CapTool(), role=role)
        result = await ex.run_command("Cap", {})
        assert result.output == "bound!"


class TestFileMutatedEmission:
    """A successful filesystem-mutating tool emits a FileMutatedEvent on the bus."""

    def _writey(self):
        from mote.executor.base_tool import BaseTool

        class WriteyTool(BaseTool):
            name = "Writey"
            mutates_filesystem = True

            def permission_target(self, args: dict) -> str:
                return args.get("path", "")

            async def call(self, *, path: str = "") -> str:
                return f"wrote {path}"

        return WriteyTool

    def _recorder(self):
        from mote.common.events import EventBus

        class Recorder(ObservationSubscriber):
            priority = 0

            def __init__(self):
                self.events = []

            async def handle(self, event):
                self.events.append(event)
                return None

        bus = EventBus()
        rec = Recorder()
        bus.subscribe(rec)
        return bus, rec

    def _executor(self, tool, bus):
        ex = ToolExecutor("sess", tools=None, bus=bus)
        tool.bind("sess")
        ex.register_tool_instance(tool, [tool.name])
        return ex

    async def test_emits_file_mutated_on_success(self):
        from mote.common.events import FileMutatedEvent

        bus, rec = self._recorder()
        ex = self._executor(self._writey()(), bus)
        result = await ex.run_command("Writey", {"path": "/tmp/x.txt"})
        assert result.success is True
        mutated = [e for e in rec.events if isinstance(e, FileMutatedEvent)]
        assert len(mutated) == 1
        assert mutated[0].path == "/tmp/x.txt"
        assert mutated[0].tool == "Writey"

    async def test_no_event_when_target_empty(self):
        from mote.common.events import FileMutatedEvent

        bus, rec = self._recorder()
        ex = self._executor(self._writey()(), bus)
        # No path => permission_target is empty => no FileMutatedEvent.
        result = await ex.run_command("Writey", {})
        assert result.success is True
        assert not [e for e in rec.events if isinstance(e, FileMutatedEvent)]

    async def test_no_event_when_tool_is_not_mutating(self):
        from mote.common.events import FileMutatedEvent

        bus, rec = self._recorder()
        ex = ToolExecutor("sess", tools=None, bus=bus)
        tool = EchoTool()
        tool.bind("sess")
        ex.register_tool_instance(tool, [tool.name])
        await ex.run_command("Echo", {"text": "hi"})
        assert not [e for e in rec.events if isinstance(e, FileMutatedEvent)]

    async def test_no_event_without_bus(self):
        # No bus wired => emission path is skipped (no crash).
        ex = ToolExecutor("sess", tools=None)
        tool = self._writey()()
        tool.bind("sess")
        ex.register_tool_instance(tool, [tool.name])
        result = await ex.run_command("Writey", {"path": "/tmp/y.txt"})
        assert result.success is True


class TestDeregisterTool:
    """``deregister_tool`` is the inverse of registration: it removes a bound tool
    by any of its names — every alias and the instance's session resources go
    together — and announces the change on the bus so the volatile views refresh."""

    def _recorder_bus(self):
        from mote.common.events import EventBus

        class Recorder(ObservationSubscriber):
            priority = 0

            def __init__(self):
                self.events = []

            async def handle(self, event):
                self.events.append(event)
                return None

        bus = EventBus()
        rec = Recorder()
        bus.subscribe(rec)
        return bus, rec

    async def test_removes_tool_and_all_aliases_together(self):
        ex = make_executor(EchoTool())
        # Echo routes under Echo/echo/Echo.run — deregister by any single alias.
        assert await ex.deregister_tool("echo") is True
        # Every name is gone; a later call to any of them fails cleanly.
        for n in ("Echo", "echo", "Echo.run"):
            result = await ex.run_command(n, {"text": "x"})
            assert result.success is False

    async def test_unbound_name_is_noop(self):
        ex = make_executor(EchoTool())
        assert await ex.deregister_tool("Nope") is False
        # The real tool is untouched.
        assert (await ex.run_command("Echo", {"text": "y"})).output == "y"

    async def test_only_the_target_instance_is_removed(self):
        ex = make_executor(EchoTool(), AddTool())
        await ex.deregister_tool("Echo")
        # A sibling tool still dispatches.
        assert (await ex.run_command("Add", {"a": 2, "b": 3})).output == "5"

    async def test_schemas_drop_the_removed_tool(self):
        ex = make_executor(EchoTool(), AddTool())
        await ex.deregister_tool("Echo")
        assert set(ex.get_tool_schemas()) == {"Add"}

    async def test_reclaims_session_resources(self):
        closed: list[str] = []

        class StatefulTool(EchoTool):
            name = "Stateful"
            aliases: list[str] = []

            def cleanup_session(self, session_id):
                closed.append(session_id)

        ex = make_executor(StatefulTool(), session_id="sess")
        await ex.deregister_tool("Stateful")
        assert closed == ["sess"]

    async def test_reconstructable_set_refreshes_after_removal(self):
        class ReconTool(EchoTool):
            name = "Recon"
            aliases = ["recon"]
            reconstructable = True

        ex = make_executor(ReconTool())
        assert ex.reconstructable_tool_names() == frozenset({"Recon", "recon"})
        await ex.deregister_tool("Recon")
        assert ex.reconstructable_tool_names() == frozenset()

    async def test_emits_tools_changed_event(self):
        from mote.common.events import ToolsChangedEvent

        bus, rec = self._recorder_bus()
        ex = ToolExecutor("sess", tools=None, bus=bus)
        tool = EchoTool()
        tool.bind("sess")
        ex.register_tool_instance(tool, [tool.name, *tool.aliases])
        await ex.deregister_tool("Echo")

        changed = [e for e in rec.events if isinstance(e, ToolsChangedEvent)]
        assert len(changed) == 1
        # The event names every alias that went away — the tool catalog frontier
        # reads this to stop announcing them.
        assert set(changed[0].removed) == {"Echo", "echo", "Echo.run"}

    async def test_event_carries_fresh_reconstructable_set(self):
        # Two reconstructable tools; removing one must leave the other's names in
        # the announced set, so a compaction consumer refreshes from the event alone.
        from mote.common.events import ToolsChangedEvent

        class ReconA(EchoTool):
            name = "ReconA"
            aliases: list[str] = []
            reconstructable = True

        class ReconB(AddTool):
            name = "ReconB"
            reconstructable = True

        bus, rec = self._recorder_bus()
        ex = ToolExecutor("sess", tools=None, bus=bus)
        for t in (ReconA(), ReconB()):
            t.bind("sess")
            ex.register_tool_instance(t, [t.name, *getattr(t, "aliases", [])])
        await ex.deregister_tool("ReconA")

        evt = [e for e in rec.events if isinstance(e, ToolsChangedEvent)][0]
        assert set(evt.removed) == {"ReconA"}
        assert set(evt.reconstructable) == {"ReconB"}

    async def test_noop_removal_emits_nothing(self):
        from mote.common.events import ToolsChangedEvent

        bus, rec = self._recorder_bus()
        ex = ToolExecutor("sess", tools=None, bus=bus)
        tool = EchoTool()
        tool.bind("sess")
        ex.register_tool_instance(tool, [tool.name])
        await ex.deregister_tool("Nope")
        assert not [e for e in rec.events if isinstance(e, ToolsChangedEvent)]


class TestRecoveryWiring:
    """``run_command`` runs the tool under the generic ``RecoveryRunner``.

    By default the registry is empty, so the loop is behaviourally identical to
    a plain ``tool.call()``. An injected strategy proves the same skeleton the
    LLM layer uses is in place for future tool-level failover.
    """

    @staticmethod
    def _flaky_tool():
        from mote.common.exception import MoteError, RecoveryAction
        from mote.executor.base_tool import BaseTool

        class _CompressError(MoteError):
            default_recovery = RecoveryAction.COMPRESS

        class FlakyTool(BaseTool):
            name = "Flaky"

            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            async def call(self) -> str:
                self.calls += 1
                if self.calls == 1:
                    raise _CompressError("first call fails recoverably")
                return "recovered"

        return FlakyTool, _CompressError

    async def test_empty_registry_does_not_retry(self):
        # Default executor has an empty strategy registry: the typed error
        # surfaces as a failed ToolResult, the tool is called exactly once.
        FlakyTool, _ = self._flaky_tool()
        tool = FlakyTool()
        ex = make_executor(tool)
        result = await ex.run_command("Flaky", {})
        assert result.success is False
        assert tool.calls == 1

    async def test_injected_strategy_recovers(self):
        from mote.common.exception import RecoveryAction

        FlakyTool, _ = self._flaky_tool()
        tool = FlakyTool()
        recovered = []

        async def compress(exc):
            recovered.append(exc)
            return True

        ex = make_executor(tool, recovery_strategies={RecoveryAction.COMPRESS: compress})
        result = await ex.run_command("Flaky", {})
        assert result.success is True
        assert result.output == "recovered"
        assert tool.calls == 2  # initial failure + one recovered retry
        assert len(recovered) == 1


class _FakeMcp:
    """A stand-in for ``UniversalMCP`` that registers a fixed adapter set.

    ``reload_mcp`` news up ``UniversalMCP()`` then calls ``initialize`` (which we
    make a no-op) and ``register_tools(executor)``. This fake registers one MCP
    adapter per configured tool name so a reload is fully observable without ever
    touching a real MCP server. ``cleanup_clients`` records that teardown ran.
    """

    #: The tool names the *next* ``UniversalMCP()`` instance will register. Set
    #: by the test before each reload to model a changed ``mcp_config.json``.
    next_tools: list[str] = []
    #: How many instances had ``cleanup_clients`` awaited (teardown count).
    cleanups: int = 0

    def __init__(self):
        self._tools = list(_FakeMcp.next_tools)

    async def initialize(self, server_names=None, servers=None):
        return None

    def register_tools(self, executor):
        for name in self._tools:
            schema = {
                "name": name,
                "description": "an mcp tool",
                "parameters": {"type": "object", "properties": {}},
            }
            executor.register_tool_instance(MCPToolAdapter(mcp=None, tool_name=name, schema=schema), [name])

    async def cleanup_clients(self):
        _FakeMcp.cleanups += 1


class TestReloadMcp:
    """``reload_mcp`` is the reentrant sibling of ``init_mcp``, driven by the file
    watcher when ``mcp_config.json`` changes. It tears the old MCP adapters out by
    identity, rebuilds from the freshly read config, and announces the churn on the
    bus so the volatile views refresh. The native channel just rebuilds tool_specs."""

    def _patch(self, monkeypatch, tools):
        from mote.executor import tool_executor as te

        _FakeMcp.next_tools = list(tools)
        _FakeMcp.cleanups = 0
        monkeypatch.setattr(te, "UniversalMCP", _FakeMcp)

    def _recorder_bus(self):
        from mote.common.events import EventBus
        from mote.common.interface.event_subscriber import ObservationSubscriber

        class Recorder(ObservationSubscriber):
            priority = 0

            def __init__(self):
                self.events = []

            async def handle(self, event):
                self.events.append(event)
                return None

        bus = EventBus()
        rec = Recorder()
        bus.subscribe(rec)
        return bus, rec

    async def test_noop_when_no_mcps_declared(self, monkeypatch):
        self._patch(monkeypatch, ["server:a"])
        ex = make_executor(EchoTool())
        assert await ex.reload_mcp(None) is False
        assert await ex.reload_mcp([]) is False
        # No MCP adapters were ever wired.
        assert ex.get_mcp_tool_schemas() == {}

    async def test_reload_registers_current_config(self, monkeypatch):
        self._patch(monkeypatch, ["server:a", "server:b"])
        ex = make_executor(EchoTool())
        assert await ex.reload_mcp(["server"]) is True
        assert set(ex.get_mcp_tool_schemas()) == {"server:a", "server:b"}
        # Built-in tools are untouched by an MCP reload.
        assert "Echo" in ex.get_tool_schemas()

    async def test_reload_swaps_old_adapters_out(self, monkeypatch):
        # First reload wires {a, b}; a second reload models a changed config
        # where b is gone and c is added — the stale adapter must not survive.
        self._patch(monkeypatch, ["server:a", "server:b"])
        ex = make_executor(EchoTool())
        await ex.reload_mcp(["server"])

        _FakeMcp.next_tools = ["server:a", "server:c"]
        await ex.reload_mcp(["server"])
        assert set(ex.get_mcp_tool_schemas()) == {"server:a", "server:c"}

    async def test_reload_tears_down_old_manager(self, monkeypatch):
        self._patch(monkeypatch, ["server:a"])
        ex = make_executor(EchoTool())
        await ex.reload_mcp(["server"])  # first: no prior manager to clean up
        assert _FakeMcp.cleanups == 0
        await ex.reload_mcp(["server"])  # second: the first manager is dropped
        assert _FakeMcp.cleanups == 1

    async def test_emits_tools_changed_event_with_removed(self, monkeypatch):
        from mote.common.events import ToolsChangedEvent

        self._patch(monkeypatch, ["server:a", "server:b"])
        bus, rec = self._recorder_bus()
        ex = ToolExecutor("sess", tools=None, bus=bus)
        await ex.reload_mcp(["server"])  # wires {a, b}, removed=[] (nothing prior)

        # Second reload drops b, so its name is announced as removed.
        _FakeMcp.next_tools = ["server:a"]
        await ex.reload_mcp(["server"])

        changed = [e for e in rec.events if isinstance(e, ToolsChangedEvent)]
        assert len(changed) == 2
        assert changed[0].removed == []
        assert set(changed[1].removed) == {"server:a", "server:b"}

    async def test_reload_is_reentrant(self, monkeypatch):
        # Running the same reload repeatedly is stable — no adapter duplication.
        self._patch(monkeypatch, ["server:a"])
        ex = make_executor(EchoTool())
        for _ in range(3):
            await ex.reload_mcp(["server"])
        assert set(ex.get_mcp_tool_schemas()) == {"server:a"}
