import json
from types import MappingProxyType

from mote.contracts.model.invocation import CanonicalToolCall
from mote.runtime.models.model_calls import canonical_tool


def test_canonical_tool_projects_deeply_frozen_schema_to_contract() -> None:
    schema = MappingProxyType(
        {
            "type": "object",
            "properties": MappingProxyType({"name": MappingProxyType({"type": "string"})}),
        }
    )

    tool = canonical_tool(
        {
            "name": "Skill",
            "description": "Invoke a skill",
            "input_schema": schema,
        }
    )

    assert tool.input_schema["properties"]["name"]["type"] == "string"
    assert json.loads(tool.model_dump_json())["input_schema"] == {
        "type": "object",
        "properties": {"name": {"type": "string"}},
    }


def test_canonical_tool_call_serializes_frozen_arguments() -> None:
    call = CanonicalToolCall(
        name="SearchTools",
        arguments={"query": "image", "filters": {"enabled": True}},
    )

    assert json.loads(call.model_dump_json())["arguments"] == {
        "query": "image",
        "filters": {"enabled": True},
    }
