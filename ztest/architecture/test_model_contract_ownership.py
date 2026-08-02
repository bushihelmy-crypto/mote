from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from mote.contracts.model.inference import InferenceResult
from mote.contracts.model.invocation import CanonicalToolCall
from mote.contracts.model.operations import ModelOperation
from mote.contracts.model.topology import EndpointCapabilityDeclaration
from mote.kernel.commands.native import NativeToolChannel
from mote.runtime.models.failover.snapshot import resolve_endpoint_capabilities

ROOT = Path(__file__).resolve().parents[2]


def test_canonical_tool_call_has_one_authoritative_definition() -> None:
    definitions = []
    for path in (ROOT / "contracts/model").rglob("*.py"):
        if "class CanonicalToolCall" in path.read_text(encoding="utf-8"):
            definitions.append(str(path.relative_to(ROOT)))

    assert definitions == ["contracts/model/invocation.py"]
    assert set(CanonicalToolCall.model_fields) == {"id", "name", "arguments"}


def test_canonical_tool_call_arguments_are_deeply_immutable() -> None:
    source = {"nested": {"items": [1]}}
    call = CanonicalToolCall(name="Read", arguments=source)
    source["nested"]["items"].append(2)

    assert call.arguments["nested"]["items"] == (1,)
    with pytest.raises(TypeError):
        call.arguments["new"] = True  # type: ignore[index]


def test_inference_result_rejects_legacy_tool_call_shape() -> None:
    with pytest.raises(ValidationError):
        InferenceResult(tool_calls=({"id": "call-1", "command_name": "Read", "args": {}},))  # type: ignore[arg-type]


def test_native_channel_consumes_authoritative_tool_call() -> None:
    result = InferenceResult(
        tool_calls=(
            CanonicalToolCall(
                id="call-1",
                name="Read",
                arguments={"path": "README.md"},
            ),
        )
    )

    turn = asyncio.run(NativeToolChannel().model_turn(result))

    action = turn.actions[0]
    assert action.name == "Read"
    assert action.arguments == {"path": "README.md"}


def test_endpoint_capability_lifecycles_have_distinct_types_and_projection() -> None:
    declaration = EndpointCapabilityDeclaration(
        supports_tools=True,
        supports_native_schema=False,
        supports_server_web_search=False,
        supports_vision=True,
        supports_pdf=False,
        supports_native_tool_search=False,
        context_tokens=128_000,
    )

    snapshot = resolve_endpoint_capabilities(declaration)

    assert type(snapshot).__name__ == "ResolvedEndpointCapabilities"
    assert snapshot.model_dump() == declaration.model_dump()
    assert type(snapshot) is not type(declaration)


def test_ambiguous_endpoint_capabilities_name_is_absent() -> None:
    offenders = []
    for path in (ROOT / "contracts/model").rglob("*.py"):
        if re.search(r"\bEndpointCapabilities\b", path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []


def test_model_operation_has_one_owner_and_projects_without_loss() -> None:
    definitions = []
    for path in (ROOT / "contracts/model").rglob("*.py"):
        if "class ModelOperation" in path.read_text(encoding="utf-8"):
            definitions.append(str(path.relative_to(ROOT)))
    assert definitions == ["contracts/model/operations.py"]

    declaration = EndpointCapabilityDeclaration(
        supported_operations=frozenset({ModelOperation.GENERATE, ModelOperation.EMBEDDING}),
        supports_tools=True,
        supports_native_schema=False,
        supports_server_web_search=False,
        supports_vision=False,
        supports_pdf=False,
        supports_native_tool_search=False,
        context_tokens=128_000,
    )
    assert resolve_endpoint_capabilities(declaration).supported_operations == (declaration.supported_operations)
