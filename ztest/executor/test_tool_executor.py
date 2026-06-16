#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for ``metagpt.executor.tool_executor.ToolExecutor``.

The dispatch tests inject already-bound instances via ``register_tool_instance``
(see ``make_executor``) so they never touch the global registry. One test
exercises the constructor's registry-prebind path using the
``restore_global_registry`` snapshot fixture.
"""
from __future__ import annotations

import pytest

from metagpt.common.exception import ToolValidationError
from metagpt.common.schema import ToolResultLimitConfig
from metagpt.executor.base_tool import BaseTool
from metagpt.executor.tasks.types import BgTaskResult
from metagpt.executor.mcp_adapter import MCPToolAdapter
from metagpt.executor.tool_executor import ToolExecutor, _validate_call_args

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


class TestRunCommandErrors:
    async def test_tool_error_becomes_failure_result(self):
        ex = make_executor(FailTool())
        result = await ex.run_command("Fail", {"message": "missing file"})
        assert result.success is False
        assert result.output == "missing file"

    async def test_generic_exception_becomes_failure_result(self):
        ex = make_executor(BoomTool())
        result = await ex.run_command("Boom", {})
        assert result.success is False
        assert "Boom" in result.output
        assert "kaboom" in result.output


class TestRunCommandArgValidation:
    """LLM-supplied args are validated against the tool's call() signature
    before dispatch (a missing required / unexpected arg becomes a structured
    failure instead of an opaque TypeError)."""

    async def test_missing_required_arg_is_validation_failure(self):
        ex = make_executor(EchoTool())
        result = await ex.run_command("Echo", {})  # `text` is required
        assert result.success is False
        assert "Echo" in result.output
        assert "missing required argument(s): text" in result.output

    async def test_unexpected_arg_is_validation_failure(self):
        ex = make_executor(EchoTool())
        result = await ex.run_command("Echo", {"text": "hi", "bogus": 1})
        assert result.success is False
        assert "unexpected argument(s): bogus" in result.output

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
        assert "missing required argument(s): text" in str(exc.value)


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

        from metagpt.executor.base_tool import BaseTool

        class BigTool(BaseTool):
            name = "Big"

            async def call(self):
                return big

        ex = make_executor(BigTool(), session_id="limit-sess")
        # Point persistence at a tmp dir via the module default base_dir.
        import metagpt.executor.tool_result_limit as trl

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

        from metagpt.executor.base_tool import BaseTool

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
        from metagpt.executor.base_tool import BaseTool

        class BigMedia(BaseTool):
            name = "BigMedia"

            async def call(self):
                from metagpt.executor.tool_result import ToolResult

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


class TestConstructorAndCleanup:
    async def test_constructor_prebinds_from_registry(self, restore_global_registry):
        # Register a test tool into the (snapshotted) global registry, then let
        # the constructor resolve it by name.
        from metagpt.executor.base_tool import BaseTool

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
        from metagpt.executor.base_tool import BaseTool

        class WriteyTool(BaseTool):
            name = "Writey"
            mutates_filesystem = True

            def permission_target(self, args: dict) -> str:
                return args.get("path", "")

            async def call(self, *, path: str = "") -> str:
                return f"wrote {path}"

        return WriteyTool

    def _recorder(self):
        from metagpt.common.events import EventBus

        class Recorder:
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
        from metagpt.common.events import FileMutatedEvent

        bus, rec = self._recorder()
        ex = self._executor(self._writey()(), bus)
        result = await ex.run_command("Writey", {"path": "/tmp/x.txt"})
        assert result.success is True
        mutated = [e for e in rec.events if isinstance(e, FileMutatedEvent)]
        assert len(mutated) == 1
        assert mutated[0].path == "/tmp/x.txt"
        assert mutated[0].tool == "Writey"

    async def test_no_event_when_target_empty(self):
        from metagpt.common.events import FileMutatedEvent

        bus, rec = self._recorder()
        ex = self._executor(self._writey()(), bus)
        # No path => permission_target is empty => no FileMutatedEvent.
        result = await ex.run_command("Writey", {})
        assert result.success is True
        assert not [e for e in rec.events if isinstance(e, FileMutatedEvent)]

    async def test_no_event_when_tool_is_not_mutating(self):
        from metagpt.common.events import FileMutatedEvent

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


class TestRecoveryWiring:
    """``run_command`` runs the tool under the generic ``RecoveryRunner``.

    By default the registry is empty, so the loop is behaviourally identical to
    a plain ``tool.call()``. An injected strategy proves the same skeleton the
    LLM layer uses is in place for future tool-level failover.
    """

    @staticmethod
    def _flaky_tool():
        from metagpt.common.exception import MetaGPTError, RecoveryAction
        from metagpt.executor.base_tool import BaseTool

        class _CompressError(MetaGPTError):
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
        from metagpt.common.exception import RecoveryAction

        FlakyTool, _ = self._flaky_tool()
        tool = FlakyTool()
        recovered = []

        async def compress(exc):
            recovered.append(exc)
            return True

        ex = make_executor(
            tool, recovery_strategies={RecoveryAction.COMPRESS: compress}
        )
        result = await ex.run_command("Flaky", {})
        assert result.success is True
        assert result.output == "recovered"
        assert tool.calls == 2  # initial failure + one recovered retry
        assert len(recovered) == 1
