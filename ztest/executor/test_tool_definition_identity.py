from __future__ import annotations

from dataclasses import replace

from mote.contracts.tool import ToolEffect
from mote.runtime.tools.base_tool import BaseTool
from mote.runtime.tools.definition_compiler import compile_tool_catalog_identity, compile_tool_definition
from mote.runtime.tools.mcp.toolsets import NativeMcpToolset
from mote.runtime.tools.mcp.types import DiscoveredMcpTool
from mote.runtime.tools.provider_definitions import NativeToolDefinition
from mote.runtime.tools.tool_binding import BoundApprovalPolicy, ExecutableToolBinding


class _Capability(BaseTool):
    name = "Alpha"
    aliases = ["a"]
    effect = ToolEffect.PURE

    async def call(self, *, value: str = ""):
        return value


def _definition(**changes):
    baseline = NativeToolDefinition(
        name="Alpha",
        aliases=("a",),
        capability_factory=_Capability,
        capability_type=_Capability,
        schema_renderer=lambda _: {
            "name": "Alpha",
            "description": "alpha",
            "input_schema": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
            },
        },
        source_identity="test:source-v1",
        description="alpha",
    )
    return replace(baseline, **changes)


def _compile(definition=None, *, approval_identity="none"):
    return compile_tool_definition(
        definition or _definition(),
        _Capability(),
        approval_identity=approval_identity,
    )


def test_semantic_identity_is_stable_for_canonical_mapping_order():
    reversed_schema = lambda _: {
        "input_schema": {
            "properties": {"value": {"type": "string"}},
            "type": "object",
        },
        "description": "alpha",
        "name": "Alpha",
    }
    assert _compile().semantic_identity == _compile(_definition(schema_renderer=reversed_schema)).semantic_identity


def test_each_authoritative_definition_field_changes_identity():
    baseline = _compile().semantic_identity
    variants = (
        _definition(name="Other"),
        _definition(aliases=("other",)),
        _definition(
            description="other",
            schema_renderer=lambda _: {
                "name": "Alpha",
                "description": "other",
                "input_schema": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                },
            },
        ),
        _definition(
            schema_renderer=lambda _: {
                "name": "Alpha",
                "description": "alpha",
                "input_schema": {"type": "object", "properties": {}},
            }
        ),
        _definition(approval_required=True),
        _definition(source_identity="test:source-v2"),
    )
    identities = [
        _compile(
            variant,
            approval_identity=("definition-required" if variant.approval_required else "none"),
        ).semantic_identity
        for variant in variants
    ]
    assert all(identity != baseline for identity in identities)


def test_effect_and_bound_approval_policy_change_identity():
    class External(_Capability):
        effect = ToolEffect.EXTERNAL

    definition = _definition(capability_type=External, capability_factory=External)
    external = compile_tool_definition(definition, External(), approval_identity="none")
    assert external.semantic_identity != _compile().semantic_identity

    policy = BoundApprovalPolicy("policy:generation-2", lambda _args: True)
    bound = ExecutableToolBinding(_definition(), _Capability(), policy)
    assert bound.semantic_identity != _compile().semantic_identity


def test_catalog_identity_is_order_independent_and_definition_sensitive():
    alpha = _compile()
    beta = _compile(_definition(name="Beta", aliases=("b",), source_identity="test:beta"))
    assert compile_tool_catalog_identity((alpha, beta)) == compile_tool_catalog_identity((beta, alpha))
    assert compile_tool_catalog_identity((alpha,)) != compile_tool_catalog_identity((beta,))


class _McpSource:
    def __init__(self, tools):
        self._tools = tools

    def discovered_tools(self):
        return self._tools

    async def call_tool(self, tool_name, parameters):
        return "ok"


def _mcp_tool(*, description="remote", source="mcp:source-v1"):
    return DiscoveredMcpTool(
        name="server:remote",
        description=description,
        input_schema={"type": "object", "properties": {}},
        source_identity=source,
    )


def test_mcp_catalog_version_tracks_discovery_content_and_source():
    baseline = NativeMcpToolset(_McpSource((_mcp_tool(),))).version
    assert NativeMcpToolset(_McpSource((_mcp_tool(description="changed"),))).version != baseline
    assert NativeMcpToolset(_McpSource((_mcp_tool(source="mcp:source-v2"),))).version != baseline
    assert NativeMcpToolset(_McpSource(())).version != baseline
