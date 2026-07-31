from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from mote.contracts.tool import CommandProtocol, ToolsetIdentity, ToolsetProtocolError
from mote.kernel.execution.run_context import RunContext
from mote.runtime.run_context import bind_run_context
from mote.runtime.tools.base_tool import BaseTool
from mote.runtime.tools.definitions import native_definition, xml_definition
from mote.runtime.tools.provider import (
    NativeToolset,
    ToolsetCompositionError,
    ToolsetConflictError,
    XmlToolset,
    resolve_tool,
    toolset_manifest,
    validate_toolset_protocols,
)
from mote.runtime.tools.tool_executor import ToolExecutor


class Echo(BaseTool):
    name = "Echo"

    async def call(self, *, text: str) -> str:
        """Return text.

        Args:
            text: Text to return.
        """

        return text


class Write(Echo):
    name = "Write"
    mutates_filesystem = True


@dataclass
class ApprovalDependencies:
    protected_values: set[str]


def test_xml_and_native_are_nominally_distinct() -> None:
    xml = XmlToolset("xml", (xml_definition(Echo),))
    native = NativeToolset("native", (native_definition(Echo),))

    with pytest.raises(ToolsetProtocolError, match="cannot compose xml Toolset with native"):
        xml.combine(native)  # type: ignore[arg-type]
    with pytest.raises(ToolsetProtocolError, match="cannot compose native Toolset with xml"):
        native.combine(xml)  # type: ignore[arg-type]


def test_toolset_identity_is_versioned_and_protocol_explicit() -> None:
    xml = XmlToolset("workspace", (), version="2026.07")
    native = NativeToolset("workspace", (), version="2026.07")

    assert xml.identity == ToolsetIdentity(
        id="workspace",
        version="2026.07",
        protocol=CommandProtocol.XML,
    )
    assert native.identity.protocol is CommandProtocol.NATIVE
    assert toolset_manifest((xml,)) == (xml.identity,)


def test_views_inherit_version_and_combined_version_tracks_children() -> None:
    source = NativeToolset("source", (), version="3")
    filtered = source.filter(lambda _definition: True)
    combined = filtered.combine(NativeToolset("other", (), version="8"))

    assert filtered.version == "3"
    assert combined.version.startswith("combined:")
    assert len(combined.version) == len("combined:") + 64
    changed = filtered.combine(NativeToolset("other", (), version="9"))
    assert changed.version != combined.version


def test_resolve_tool_rejects_cross_protocol_sources() -> None:
    xml = XmlToolset("xml", (xml_definition(Echo),))
    native = NativeToolset("native", (native_definition(Echo),))

    with pytest.raises(ToolsetProtocolError):
        resolve_tool((xml, native), "Echo")  # type: ignore[arg-type]


def test_same_protocol_composition_is_immutable_and_detects_conflicts() -> None:
    source = NativeToolset("source", (native_definition(Echo), native_definition(Write)))
    workspace = source.filter(lambda definition: definition.capability_type is Write).prefix("workspace")

    assert workspace.tool_names() == ("workspace_Write",)
    assert source.tool_names() == ("Echo", "Write")

    conflict = NativeToolset("one", (native_definition(Echo),)).combine(
        NativeToolset("two", (native_definition(Echo),))
    )
    with pytest.raises(ToolsetConflictError):
        conflict.definitions()


def test_static_iterable_source_is_repeatable() -> None:
    definitions = (definition for definition in (native_definition(Echo),))
    tools = NativeToolset("repeatable", definitions)

    assert tools.tool_names() == ("Echo",)
    assert tools.tool_names() == ("Echo",)


def test_static_instructions_compose_immutably_and_deduplicate() -> None:
    source = NativeToolset(
        "source",
        (native_definition(Echo),),
        instructions="Use Echo for exact repetition.",
    )
    instructed = source.with_instructions(
        "Keep responses short.",
        "Use Echo for exact repetition.",
    )
    combined = instructed.combine(
        NativeToolset(
            "write",
            (native_definition(Write),),
            instructions="Keep responses short.",
        )
    )

    assert source.static_instruction_blocks == ("Use Echo for exact repetition.",)
    assert instructed.static_instruction_blocks == (
        "Use Echo for exact repetition.",
        "Keep responses short.",
    )
    assert combined.static_instruction_blocks == instructed.static_instruction_blocks


@pytest.mark.asyncio
async def test_renamed_native_tool_preserves_source_and_dispatches() -> None:
    source = NativeToolset("source", (native_definition(Echo),))
    renamed = source.rename({"Echo": "Respond"})
    executor = ToolExecutor(
        "session",
        tools=["Respond"],
        toolsets=(renamed,),
        command_protocol="native",
    )

    assert source.tool_names() == ("Echo",)
    assert renamed.tool_names() == ("Respond",)
    assert executor.native_tool_specs()[0]["name"] == "Respond"
    result = await executor.run_command("Respond", {"text": "ok"})
    assert result.success is True
    assert result.output == "ok"


def test_rename_rejects_invalid_or_unknown_names() -> None:
    tools = XmlToolset("xml", (xml_definition(Echo),))

    with pytest.raises(ToolsetCompositionError, match="target.*must not be empty"):
        tools.rename({"Echo": " "})
    with pytest.raises(ToolsetCompositionError, match="unknown tools.*Missing"):
        tools.rename({"Missing": "Other"}).definitions()


def test_prepared_view_may_change_definition_metadata_or_remove_tools() -> None:
    source = NativeToolset("source", (native_definition(Echo), native_definition(Write)))
    prepared = source.prepared(
        lambda definitions: (
            replace(definition, description="prepared") for definition in definitions if definition.name == "Echo"
        )
    )

    assert prepared.tool_names() == ("Echo",)
    assert prepared.definitions()[0].description == "prepared"
    assert source.tool_names() == ("Echo", "Write")


def test_prepared_view_cannot_add_rename_or_replace_capabilities() -> None:
    source_definition = native_definition(Echo)
    source = NativeToolset("source", (source_definition,))

    with pytest.raises(ToolsetCompositionError, match="cannot add or rename"):
        source.prepared(lambda definitions: (replace(definitions[0], name="Other"),)).definitions()
    with pytest.raises(ToolsetCompositionError, match="cannot replace the capability"):
        source.prepared(lambda definitions: (replace(definitions[0], capability_factory=lambda: Echo()),)).definitions()
    approved = source.with_approval()
    assert approved.prepared(lambda definitions: definitions).requires_permission_gate is True


def test_agent_composition_rejects_duplicate_ids_and_cross_toolset_names() -> None:
    first = NativeToolset("same", (native_definition(Echo),))
    duplicate_id = NativeToolset("same", (native_definition(Write),))
    duplicate_name = NativeToolset("other", (native_definition(Echo),))

    with pytest.raises(ToolsetConflictError, match="id 'same'.*more than once"):
        validate_toolset_protocols("native", (first, duplicate_id))
    executor = ToolExecutor(
        "session",
        tools=["Echo"],
        toolsets=(first, duplicate_name),
        command_protocol="native",
    )
    with pytest.raises(ToolsetConflictError, match="provided by both Toolset"):
        executor.prepare()


@pytest.mark.asyncio
async def test_prefixed_native_tool_runs_through_shared_executor_pipeline() -> None:
    tools = NativeToolset("source", (native_definition(Echo),)).prefix("workspace")
    executor = ToolExecutor(
        "session",
        tools=["workspace_Echo"],
        toolsets=(tools,),
        command_protocol="native",
    )

    assert executor.native_tool_specs()[0]["name"] == "workspace_Echo"
    result = await executor.run_command("workspace_Echo", {"text": "ok"})
    assert result.success is True
    assert result.output == "ok"


def test_catalogs_do_not_cross_project() -> None:
    native = ToolExecutor(
        "native",
        tools=["Echo"],
        toolsets=(NativeToolset("n", (native_definition(Echo),)),),
        command_protocol="native",
    )
    xml = ToolExecutor(
        "xml",
        tools=["Echo"],
        toolsets=(XmlToolset("x", (xml_definition(Echo),)),),
        command_protocol="xml",
    )

    with pytest.raises(TypeError, match="no XML"):
        native.all_xml_tool_schemas()
    with pytest.raises(TypeError, match="no Native"):
        xml.native_tool_specs()


@pytest.mark.asyncio
async def test_approval_view_still_uses_central_permission_gate() -> None:
    tools = NativeToolset("write", (native_definition(Write),)).with_approval(mutating_only=True)
    executor = ToolExecutor(
        "session",
        tools=["Write"],
        toolsets=(tools,),
        command_protocol="native",
    )
    result = await executor.run_command("Write", {"text": "blocked"})
    assert result.success is False
    assert "approval" in result.output.lower()


@pytest.mark.asyncio
async def test_typed_approval_policy_can_inspect_run_deps_definition_and_args() -> None:
    seen: list[tuple[str, str]] = []

    def policy(ctx, definition, args) -> bool:
        value = str(args["text"])
        seen.append((definition.name, value))
        return value in ctx.deps.protected_values

    tools = NativeToolset[ApprovalDependencies](
        "echo",
        (native_definition(Echo),),
    ).with_approval(policy)
    executor = ToolExecutor(
        "session",
        tools=["Echo"],
        toolsets=(tools,),
        command_protocol="native",
    )
    ctx = RunContext(
        deps=ApprovalDependencies(protected_values={"secret"}),
        session_id="session",
        run_id="run",
    )

    with bind_run_context(ctx):
        await executor.start_run(ctx)
        allowed = await executor.run_command("Echo", {"text": "public"})
        blocked = await executor.run_command("Echo", {"text": "secret"})
        await executor.end_run()

    assert allowed.output == "public"
    assert blocked.success is False
    assert "approval" in blocked.output.lower()
    assert seen == [("Echo", "public"), ("Echo", "secret")]


@pytest.mark.asyncio
async def test_approval_policy_fails_closed_without_active_run_context() -> None:
    called = False

    def policy(_ctx, _definition, _args) -> bool:
        nonlocal called
        called = True
        return False

    tools = NativeToolset[ApprovalDependencies](
        "echo",
        (native_definition(Echo),),
    ).with_approval(policy)
    executor = ToolExecutor(
        "session",
        tools=["Echo"],
        toolsets=(tools,),
        command_protocol="native",
    )

    result = await executor.run_command("Echo", {"text": "public"})

    assert result.success is False
    assert "approval" in result.output.lower()
    assert called is False


def test_async_approval_policy_is_rejected_at_composition_boundary() -> None:
    async def policy(_ctx, _definition, _args) -> bool:
        return True

    tools = NativeToolset[ApprovalDependencies](
        "echo",
        (native_definition(Echo),),
    )

    with pytest.raises(ToolsetCompositionError, match="must be synchronous"):
        tools.with_approval(policy)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_approval_policy_cannot_mutate_dispatched_arguments() -> None:
    def policy(_ctx, _definition, args) -> bool:
        args["text"] = "rewritten"  # type: ignore[index]
        return False

    tools = NativeToolset[ApprovalDependencies](
        "echo",
        (native_definition(Echo),),
    ).with_approval(policy)
    executor = ToolExecutor(
        "session",
        tools=["Echo"],
        toolsets=(tools,),
        command_protocol="native",
    )
    ctx = RunContext(
        deps=ApprovalDependencies(protected_values=set()),
        session_id="session",
        run_id="run",
    )

    with bind_run_context(ctx):
        result = await executor.run_command("Echo", {"text": "original"})

    assert result.success is False
    assert "permission" in result.output.lower()
