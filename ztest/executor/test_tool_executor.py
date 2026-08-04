#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for ``mote.runtime.tools.tool_executor.ToolExecutor``.

The dispatch tests inject already-bound instances via ``register_tool_instance``
(see ``make_executor``) so they never touch the global registry. One test
exercises the constructor's registry-prebind path using the
immutable catalog fixtures.
"""

from __future__ import annotations

import asyncio

import pytest

from mote.contracts.config.tool import ToolResultLimitConfig
from mote.contracts.events.tool import ToolCallFinishedEvent, ToolInvocationStartedEvent
from mote.contracts.tool import ToolEffect
from mote.orchestration.background_tasks.model import BgTaskResult
from mote.runtime.tools.base_tool import BaseTool
from mote.runtime.tools.definitions import native_definition
from mote.runtime.tools.mcp.adapter import MCPToolAdapter
from mote.runtime.tools.mcp.types import DiscoveredMcpTool
from mote.runtime.tools.tool_executor import ToolExecutor
from mote.runtime.tools.tool_registry import NativeCatalogToolset
from mote.runtime.tools.tool_result import FileChange, ToolResult
from mote.runtime.tools.tool_result_receipt import decode_tool_result_receipt
from mote.ztest.artifact_fakes import artifact_media
from mote.ztest.telemetry import InlineTelemetry

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


def _register_native(executor: ToolExecutor, tool: BaseTool) -> None:
    executor.register_native_tool(native_definition(type(tool)), tool)


class _EventRecorder:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def handle(self, event: object) -> None:
        self.events.append(event)


async def test_execution_owner_mints_one_identity_for_start_and_finish() -> None:
    recorder = _EventRecorder()
    executor = make_executor(EchoTool(), telemetry=InlineTelemetry(recorder))

    result = await executor.run_command("Echo", {"text": "hello"})

    assert result.success is True
    started = next(event for event in recorder.events if isinstance(event, ToolInvocationStartedEvent))
    finished = next(event for event in recorder.events if isinstance(event, ToolCallFinishedEvent))
    assert started.identity == finished.identity
    assert str(started.identity.invocation_id).startswith("tool-")
    assert int(started.identity.attempt_ordinal) == 1
    assert started.identity.definition_identity.startswith("sha256-")


async def test_explicit_logical_identity_advances_attempt_ordinal() -> None:
    class PureEcho(EchoTool):
        effect = ToolEffect.PURE

    recorder = _EventRecorder()
    executor = make_executor(PureEcho(), telemetry=InlineTelemetry(recorder))

    await executor.run_command("Echo", {"text": "one"}, result_id="logical-call")
    await executor.run_command("Echo", {"text": "two"}, result_id="logical-call")

    starts = [event for event in recorder.events if isinstance(event, ToolInvocationStartedEvent)]
    assert [int(event.identity.attempt_ordinal) for event in starts] == [1, 2]
    assert starts[0].identity.arguments_digest != starts[1].identity.arguments_digest


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
    """Python invocation errors are normalized at the executor boundary."""

    async def test_missing_required_arg_is_validation_failure(self):
        ex = make_executor(EchoTool())
        result = await ex.run_command("Echo", {})  # `text` is required
        assert result.success is False
        assert "Echo" in result.output
        assert "missing 1 required keyword-only argument: 'text'" in result.output

    async def test_unexpected_arg_is_validation_failure(self):
        ex = make_executor(EchoTool())
        result = await ex.run_command("Echo", {"text": "hi", "bogus": 1})
        assert result.success is False
        assert "unexpected keyword argument 'bogus'" in result.output

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


class TestRunCommandReturnNormalization:
    async def test_structured_toolresult_passthrough(self):
        ex = make_executor(StructuredResultTool())
        result = await ex.run_command("Struct", {"ok": True})
        assert result.success is True
        assert result.output == "structured"
        assert result.payload is not None
        assert result.payload.materialize() == {"k": "v"}

    async def test_bg_task_result_is_process_local_execution_value(self):
        ex = make_executor(BgTool())
        result = await ex.run_command("Bg", {"label": "crawl"})
        assert result.success is True
        assert result.output == "started"
        assert isinstance(result.execution_value, BgTaskResult)
        assert result.execution_value.command_name == "crawl"
        assert result.payload is None

    async def test_atomic_tool_cannot_return_deferred_work(self):
        class InvalidDeferredTool(BaseTool):
            name = "InvalidDeferred"

            async def call(self):
                return BgTaskResult.foreground("unexpected")

        result = await make_executor(InvalidDeferredTool()).run_command("InvalidDeferred", {})
        assert result.success is False
        assert "atomic tool" in result.output

    async def test_media_result_passthrough(self):
        ex = make_executor(MediaTool())
        result = await ex.run_command("Media", {"payload": "BASE64"})
        assert result.media[0].artifact.size == len(b"BASE64")
        assert result.output == "Read image (1KB)"


class TestResultLimiting:
    async def test_large_output_persisted(self, tmp_path):
        big = "x" * 60_000  # over DEFAULT_MAX_RESULT_SIZE_CHARS (50k)

        from mote.runtime.tools.base_tool import BaseTool

        class BigTool(BaseTool):
            name = "Big"

            async def call(self):
                return big

        # Point persistence at a tmp dir by injecting a SessionWorkspace rooted
        # there; the persisted result co-locates under the session directory.
        from mote.runtime.session.workspace import SessionWorkspace

        ex = make_executor(
            BigTool(),
            session_id="limit-sess",
            workspace_store=SessionWorkspace(tmp_path),
        )
        result = await ex.run_command("Big", {}, result_id="rid-1")
        assert result.output.startswith("<persisted-output>")
        assert (tmp_path / ".agent_sessions" / "limit-sess" / "tool_results" / "rid-1.txt").exists()

    async def test_limiting_disabled_passes_through(self):
        big = "y" * 60_000

        from mote.runtime.tools.base_tool import BaseTool

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
        from mote.runtime.tools.base_tool import BaseTool

        class BigMedia(BaseTool):
            name = "BigMedia"

            async def call(self):
                return ToolResult(
                    output="z" * 60_000,
                    media=[artifact_media("image", "img")],
                )

        ex = make_executor(BigMedia())
        result = await ex.run_command("BigMedia", {})
        assert len(result.output) == 60_000
        assert not result.output.startswith("<persisted-output>")


class TestPersistLargeArgs:
    """The arguments twin of result-output limiting.

    ``persist_large_args`` runs a tool call's RECORDED args through the same
    persist policy/session/store the result path uses, so a giant arg blob is
    spilled to disk before the assistant message enters context. Shares the
    ``{call_id}-args`` id namespace with the compaction spill reducer.
    """

    async def test_large_args_replaced_by_envelope(self, tmp_path):
        from mote.runtime.session.workspace import SessionWorkspace

        ex = make_executor(session_id="args-sess", workspace_store=SessionWorkspace(tmp_path))
        big = {"new_string": "x" * 60_000}
        out = ex.persist_large_args(big, "call-1")
        assert isinstance(out, str)
        assert out.startswith("<persisted-output>")
        assert (tmp_path / ".agent_sessions" / "args-sess" / "tool_results" / "call-1-args.txt").exists()

    async def test_small_args_returned_unchanged(self, tmp_path):
        from mote.runtime.session.workspace import SessionWorkspace

        ex = make_executor(session_id="args-sess", workspace_store=SessionWorkspace(tmp_path))
        small = {"path": "a.py"}
        # Same dict object back (identity), not a re-serialized string.
        assert ex.persist_large_args(small, "call-1") is small

    async def test_disabled_returns_args_unchanged(self):
        ex = make_executor(limit_config=ToolResultLimitConfig(enable_tool_result_limit=False))
        big = {"new_string": "x" * 60_000}
        assert ex.persist_large_args(big, "call-1") is big

    async def test_string_args_over_threshold_persisted(self, tmp_path):
        from mote.runtime.session.workspace import SessionWorkspace

        ex = make_executor(session_id="args-sess", workspace_store=SessionWorkspace(tmp_path))
        out = ex.persist_large_args("y" * 60_000, "call-2")
        assert out.startswith("<persisted-output>")

    async def test_idempotent_already_persisted_args(self, tmp_path):
        from mote.runtime.session.workspace import SessionWorkspace

        ex = make_executor(session_id="args-sess", workspace_store=SessionWorkspace(tmp_path))
        big = {"new_string": "x" * 60_000}
        first = ex.persist_large_args(big, "call-1")
        # Feeding the envelope back (as a spill pass would) is a no-op.
        assert ex.persist_large_args(first, "call-1") == first


class TestSchemas:
    async def test_native_specs_deduplicate_aliases(self):
        ex = make_executor(EchoTool())
        specs = ex.native_tool_specs()
        assert [spec["name"] for spec in specs] == ["Echo"]

    async def test_native_specs_include_multiple_tools(self):
        ex = make_executor(EchoTool(), AddTool())
        specs = ex.native_tool_specs()
        assert {spec["name"] for spec in specs} == {"Echo", "Add"}

    async def test_native_tool_specs_anthropic(self):
        ex = make_executor(AddTool())
        specs = ex.native_tool_specs(provider="anthropic")
        assert len(specs) == 1
        spec = specs[0]
        assert spec["name"] == "Add"
        assert spec["input_schema"]["type"] == "object"

    async def test_native_tool_specs_openai(self):
        ex = make_executor(AddTool())
        specs = ex.native_tool_specs(provider="openai")
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
        return MCPToolAdapter.from_discovery(
            _FakeMcp(),
            DiscoveredMcpTool(name, "an mcp tool", schema["parameters"], "test:mcp-source"),
        )

    async def test_mcp_definition_registers_only_on_native_surface(self):
        ex = make_executor(EchoTool())
        adapter = self._adapter()
        ex.register_native_tool(adapter.native_definition(), adapter)
        assert {spec["name"] for spec in ex.native_tool_specs()} == {
            "Echo",
            "server:tool",
        }
        assert ex._catalog.category(ex._get_tool("server:tool")) == "mcp"


class TestPipelineFiltering:
    """Workflow classification is an explicit immutable definition property."""

    def _pipeline_tool(self, name="Pipe"):
        from mote.contracts.tool.execution import ToolExecutionKind

        class PipelineTool(BaseTool):
            execution_kind = ToolExecutionKind.WORKFLOW_DEFERRED

            async def call(self, **kwargs):
                return None

        PipelineTool.name = name
        return PipelineTool()

    async def test_pipeline_uses_same_native_execution_surface(self):
        ex = make_executor(EchoTool(), self._pipeline_tool())
        assert {spec["name"] for spec in ex.native_tool_specs()} == {"Echo", "Pipe"}
        assert ex._catalog.category(ex._get_tool("Pipe")) == "pipeline"


class TestPipelinesEnabledGate:
    """The bggraph master switch (``config.context.bggraph.enabled``) gates
    pipeline tool *loading* at construction: off → the pipeline tool is never
    bound, so it appears in no schema view (neither askllm's native set nor the
    XML catalog). Non-pipeline tools are unaffected."""

    def _pipeline_cls(self, name="RegPipe"):
        from mote.contracts.tool.execution import ToolExecutionKind

        class RegPipelineTool(BaseTool):
            execution_kind = ToolExecutionKind.WORKFLOW_DEFERRED

            async def call(self, **kwargs):
                return None

        RegPipelineTool.name = name
        return RegPipelineTool

    async def test_switch_off_skips_pipeline_tool(self, fresh_catalog):
        catalog = fresh_catalog.with_types(self._pipeline_cls())
        toolset = NativeCatalogToolset(id="test", catalog=catalog)
        ex = ToolExecutor("sess", tools=["RegPipe"], pipelines_enabled=False, toolsets=(toolset,))
        # Never bound → absent from every schema view.
        assert "RegPipe" not in {spec["name"] for spec in ex.native_tool_specs()}
        assert "RegPipe" not in ex.tool_names()

    async def test_switch_on_loads_pipeline_tool(self, fresh_catalog):
        catalog = fresh_catalog.with_types(self._pipeline_cls())
        toolset = NativeCatalogToolset(id="test", catalog=catalog)
        ex = ToolExecutor("sess", tools=["RegPipe"], pipelines_enabled=True, toolsets=(toolset,))
        assert {spec["name"] for spec in ex.native_tool_specs()} == {"RegPipe"}

    async def test_switch_off_keeps_non_pipeline_tools(self, fresh_catalog):
        from mote.runtime.tools.base_tool import BaseTool as _BT

        class PlainTool(_BT):
            name = "Plain"

            async def call(self, **kwargs):
                return None

        catalog = fresh_catalog.with_types(PlainTool, self._pipeline_cls())
        toolset = NativeCatalogToolset(id="test", catalog=catalog)
        ex = ToolExecutor(
            "sess",
            tools=["Plain", "RegPipe"],
            pipelines_enabled=False,
            toolsets=(toolset,),
        )
        # The non-pipeline tool is still bound; only the pipeline one is dropped.
        assert {spec["name"] for spec in ex.native_tool_specs()} == {"Plain"}


class TestConstructorAndCleanup:
    async def test_first_use_binds_from_catalog(self, fresh_catalog):
        from mote.runtime.tools.base_tool import BaseTool

        class RegTool(BaseTool):
            name = "RegTool"
            aliases = ["rt"]

            async def call(self, *, v: str = "v") -> str:
                return v

        toolset = NativeCatalogToolset(id="test", catalog=fresh_catalog.with_types(RegTool))
        ex = ToolExecutor("sess", tools=["RegTool"], toolsets=(toolset,))
        result = await ex.run_command("RegTool", {"v": "hello"})
        assert result.output == "hello"
        # Alias also routes to the same instance.
        assert await ex.run_command("rt", {"v": "z"}) is not None

    async def test_constructor_does_not_discover_or_instantiate_tools(self, fresh_catalog):
        created = 0

        class LazyTool(BaseTool):
            name = "Lazy"

            def __init__(self):
                nonlocal created
                super().__init__()
                created += 1

            async def call(self) -> str:
                return "ready"

        discover_calls = 0

        def discover():
            nonlocal discover_calls
            discover_calls += 1

        toolset = NativeCatalogToolset(
            id="test",
            catalog=fresh_catalog.with_types(LazyTool),
            prepare=discover,
        )
        executor = ToolExecutor("sess", tools=["Lazy"], toolsets=(toolset,))
        assert discover_calls == 0
        assert created == 0

        assert (await executor.run_command("Lazy", {})).output == "ready"
        assert discover_calls == 1
        assert created == 1

    async def test_unknown_declared_tool_is_skipped(self):
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
    """A mutating tool emits a successful FileMutatedEvent on Telemetry."""

    def _writey(self):
        from mote.runtime.tools.base_tool import BaseTool

        class WriteyTool(BaseTool):
            name = "Writey"
            mutates_filesystem = True

            def permission_target(self, args: dict) -> str:
                return args.get("path", "")

            async def call(self, *, path: str = "") -> str:
                return f"wrote {path}"

        return WriteyTool

    def _recorder(self):
        class Recorder:
            def __init__(self):
                self.events = []

            async def handle(self, event):
                self.events.append(event)
                return None

        rec = Recorder()
        return InlineTelemetry(rec), rec

    def _executor(self, tool, telemetry):
        ex = ToolExecutor("sess", tools=None, telemetry=telemetry)
        tool.bind("sess")
        _register_native(ex, tool)
        return ex

    async def test_emits_file_mutated_on_success(self):
        from mote.contracts.events.file.observation import FileMutatedEvent

        telemetry, rec = self._recorder()
        ex = self._executor(self._writey()(), telemetry)
        result = await ex.run_command("Writey", {"path": "/tmp/x.txt"})
        assert result.success is True
        mutated = [e for e in rec.events if isinstance(e, FileMutatedEvent)]
        assert len(mutated) == 1
        assert mutated[0].path == "/tmp/x.txt"
        assert mutated[0].tool == "Writey"

    async def test_no_event_when_target_empty(self):
        from mote.contracts.events.file.observation import FileMutatedEvent

        telemetry, rec = self._recorder()
        ex = self._executor(self._writey()(), telemetry)
        # No path => permission_target is empty => no FileMutatedEvent.
        result = await ex.run_command("Writey", {})
        assert result.success is True
        assert not [e for e in rec.events if isinstance(e, FileMutatedEvent)]

    async def test_no_event_when_tool_is_not_mutating(self):
        from mote.contracts.events.file.observation import FileMutatedEvent

        telemetry, rec = self._recorder()
        ex = ToolExecutor("sess", tools=None, telemetry=telemetry)
        tool = EchoTool()
        tool.bind("sess")
        _register_native(ex, tool)
        await ex.run_command("Echo", {"text": "hi"})
        assert not [e for e in rec.events if isinstance(e, FileMutatedEvent)]

    async def test_no_event_without_telemetry(self):
        # No Telemetry wired => emission is skipped without failing the tool.
        ex = ToolExecutor("sess", tools=None)
        tool = self._writey()()
        tool.bind("sess")
        _register_native(ex, tool)
        result = await ex.run_command("Writey", {"path": "/tmp/y.txt"})
        assert result.success is True


class TestDeregisterTool:
    """``deregister_tool`` is the inverse of registration: it removes a bound tool
    by any of its names — every alias and the instance's session resources go
    together — and announces the change on Telemetry so volatile views refresh."""

    def _recording_telemetry(self):
        class Recorder:
            def __init__(self):
                self.events = []

            async def handle(self, event):
                self.events.append(event)
                return None

        rec = Recorder()
        return InlineTelemetry(rec), rec

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
        assert {spec["name"] for spec in ex.native_tool_specs()} == {"Add"}

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

    async def test_awaits_async_session_cleanup(self):
        closed: list[str] = []

        class StatefulTool(EchoTool):
            name = "AsyncStateful"
            aliases: list[str] = []

            async def cleanup_session(self, session_id):
                await asyncio.sleep(0)
                closed.append(session_id)

        ex = make_executor(StatefulTool(), session_id="sess")
        await ex.deregister_tool("AsyncStateful")
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
        from mote.contracts.events.tool import ToolsChangedEvent

        telemetry, rec = self._recording_telemetry()
        ex = ToolExecutor("sess", tools=None, telemetry=telemetry)
        tool = EchoTool()
        tool.bind("sess")
        _register_native(ex, tool)
        await ex.deregister_tool("Echo")

        changed = [e for e in rec.events if isinstance(e, ToolsChangedEvent)]
        assert len(changed) == 1
        # The event names every alias that went away — the tool catalog frontier
        # reads this to stop announcing them.
        assert set(changed[0].removed) == {"Echo", "echo", "Echo.run"}

    async def test_event_carries_fresh_reconstructable_set(self):
        # Two reconstructable tools; removing one must leave the other's names in
        # the announced set, so a compaction consumer refreshes from the event alone.
        from mote.contracts.events.tool import ToolsChangedEvent

        class ReconA(EchoTool):
            name = "ReconA"
            aliases: list[str] = []
            reconstructable = True

        class ReconB(AddTool):
            name = "ReconB"
            reconstructable = True

        telemetry, rec = self._recording_telemetry()
        ex = ToolExecutor("sess", tools=None, telemetry=telemetry)
        for t in (ReconA(), ReconB()):
            t.bind("sess")
            _register_native(ex, t)
        await ex.deregister_tool("ReconA")

        evt = [e for e in rec.events if isinstance(e, ToolsChangedEvent)][0]
        assert set(evt.removed) == {"ReconA"}
        assert set(evt.reconstructable) == {"ReconB"}

    async def test_noop_removal_emits_nothing(self):
        from mote.contracts.events.tool import ToolsChangedEvent

        telemetry, rec = self._recording_telemetry()
        ex = ToolExecutor("sess", tools=None, telemetry=telemetry)
        tool = EchoTool()
        tool.bind("sess")
        _register_native(ex, tool)
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
        from mote.contracts.foundation.errors.base import MoteError
        from mote.contracts.foundation.errors.codes import RecoveryAction
        from mote.runtime.tools.base_tool import BaseTool

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
        from mote.contracts.foundation.errors.codes import RecoveryAction

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
    """A stand-in for the shared MCP discovery/connection owner."""

    #: The tool names the *next* ``UniversalMCP()`` instance will register. Set
    #: by the test before each reload to model a changed ``mcp_config.json``.
    next_tools: list[str] = []
    #: How many instances had ``cleanup_clients`` awaited (teardown count).
    cleanups: int = 0

    def __init__(self, *, servers=None, oauth_root=None):
        self._tools = list(_FakeMcp.next_tools)

    async def initialize(self, server_names=None, servers=None):
        return None

    def discovered_tools(self):
        return tuple(
            DiscoveredMcpTool(
                name=name,
                description="an mcp tool",
                input_schema={"type": "object", "properties": {}},
                source_identity="test:mcp-source",
            )
            for name in self._tools
        )

    async def call_tool(self, tool_name, parameters):
        return f"{tool_name}:{parameters}"

    async def cleanup_clients(self):
        _FakeMcp.cleanups += 1


class TestReloadMcp:
    """``reload_mcp`` is the reentrant sibling of ``init_mcp``, driven by the file
    watcher when ``mcp_config.json`` changes. It tears the old MCP adapters out by
    identity, rebuilds from the freshly read config, and announces the churn on the
    Telemetry so volatile views refresh. The native channel rebuilds tool_specs."""

    def _patch(self, monkeypatch, tools):
        # UniversalMCP is constructed inside the McpLifecycle collaborator, so the
        # fake is swapped in there (the executor delegates its MCP slot to it).
        from mote.runtime.tools.mcp import lifecycle as mcp_lifecycle

        _FakeMcp.next_tools = list(tools)
        _FakeMcp.cleanups = 0
        monkeypatch.setattr(mcp_lifecycle, "UniversalMCP", _FakeMcp)

    def _recording_telemetry(self):
        class Recorder:
            def __init__(self):
                self.events = []

            async def handle(self, event):
                self.events.append(event)
                return None

        rec = Recorder()
        return InlineTelemetry(rec), rec

    async def test_noop_when_switch_off(self, monkeypatch):
        # The ``config.mcp.enabled`` switch is the sole gate: with it off, a
        # reload is a no-op even when the role lists servers.
        self._patch(monkeypatch, ["server:a"])
        ex = make_executor(EchoTool())
        assert await ex.reload_mcp(None) is False
        assert await ex.reload_mcp(["server"]) is False
        # No MCP adapters were ever wired.
        assert ex._catalog.mcp_names() == []

    async def test_reload_registers_current_config(self, monkeypatch):
        self._patch(monkeypatch, ["server:a", "server:b"])
        ex = make_executor(EchoTool())
        assert await ex.reload_mcp(["server"], enabled=True) is True
        assert set(ex._catalog.mcp_names()) == {"server:a", "server:b"}
        assert set(ex.mcp_tool_schemas()) == {"server:a", "server:b"}
        # Built-in tools are untouched by an MCP reload.
        assert "Echo" in {spec["name"] for spec in ex.native_tool_specs()}

    async def test_xml_reload_uses_xml_projection_and_reminder_catalog(self, monkeypatch):
        self._patch(monkeypatch, ["server:a", "server:b"])
        ex = ToolExecutor("xml-mcp", command_protocol="xml")
        assert await ex.reload_mcp(["server"], enabled=True) is True
        assert set(ex.mcp_tool_schemas()) == {"server:a", "server:b"}
        with pytest.raises(TypeError, match="no Native tool specs"):
            ex.native_tool_specs()

    async def test_reload_swaps_old_adapters_out(self, monkeypatch):
        # First reload wires {a, b}; a second reload models a changed config
        # where b is gone and c is added — the stale adapter must not survive.
        self._patch(monkeypatch, ["server:a", "server:b"])
        ex = make_executor(EchoTool())
        await ex.reload_mcp(["server"], enabled=True)

        _FakeMcp.next_tools = ["server:a", "server:c"]
        await ex.reload_mcp(["server"], enabled=True)
        assert set(ex._catalog.mcp_names()) == {"server:a", "server:c"}

    async def test_reload_tears_down_old_manager(self, monkeypatch):
        self._patch(monkeypatch, ["server:a"])
        ex = make_executor(EchoTool())
        await ex.reload_mcp(["server"], enabled=True)  # first: no prior manager to clean up
        assert _FakeMcp.cleanups == 0
        await ex.reload_mcp(["server"], enabled=True)  # second: the first manager is dropped
        assert _FakeMcp.cleanups == 1

    async def test_emits_tools_changed_event_with_removed(self, monkeypatch):
        from mote.contracts.events.tool import ToolsChangedEvent

        self._patch(monkeypatch, ["server:a", "server:b"])
        telemetry, rec = self._recording_telemetry()
        ex = ToolExecutor("sess", tools=None, telemetry=telemetry)
        await ex.reload_mcp(["server"], enabled=True)  # wires {a, b}, removed=[] (nothing prior)

        # Second reload drops b, so its name is announced as removed.
        _FakeMcp.next_tools = ["server:a"]
        await ex.reload_mcp(["server"], enabled=True)

        changed = [e for e in rec.events if isinstance(e, ToolsChangedEvent)]
        assert len(changed) == 2
        assert changed[0].removed == []
        assert changed[0].added == ["server:a", "server:b"]
        assert changed[0].changed == []
        assert changed[1].removed == ["server:b"]
        assert changed[1].added == []
        assert changed[1].changed == ["server:a"]
        assert changed[1].generation > changed[0].generation

    async def test_reload_is_reentrant(self, monkeypatch):
        # Running the same reload repeatedly is stable — no adapter duplication.
        self._patch(monkeypatch, ["server:a"])
        ex = make_executor(EchoTool())
        for _ in range(3):
            await ex.reload_mcp(["server"], enabled=True)
