from __future__ import annotations

import ast
from pathlib import Path

from mote.runtime.tools.tool_result import ToolResult
from mote.runtime.tools.tool_result_receipt import decode_tool_result_receipt, encode_tool_result_receipt

ROOT = Path(__file__).resolve().parents[2]


def test_tool_result_has_separate_durable_and_process_local_values() -> None:
    fields = ToolResult.__dataclass_fields__

    assert "data" not in fields
    assert fields["payload"].type == "ToolPayload | None"
    assert fields["execution_value"].type == "object | None"


def test_production_has_no_legacy_tool_result_data_constructor() -> None:
    offenders: list[str] = []
    for package in ("kernel", "runtime", "orchestration", "product"):
        for path in (ROOT / package).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if any(keyword.arg == "data" for keyword in node.keywords):
                    function = node.func
                    name = (
                        function.id
                        if isinstance(function, ast.Name)
                        else (function.attr if isinstance(function, ast.Attribute) else "")
                    )
                    if name == "ToolResult":
                        offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []


def test_receipt_codec_has_no_unknown_object_repr_fallback() -> None:
    source = (ROOT / "runtime/tools/tool_result_receipt.py").read_text(encoding="utf-8")

    assert '"tool-result+json@4"' in source
    assert "_OBJECT_TAG" not in source
    assert "repr(value)" not in source


def test_unregistered_durable_payload_fails_closed() -> None:
    result = ToolResult(output="value")
    object.__setattr__(result, "payload", object())

    try:
        encode_tool_result_receipt(result)
    except TypeError as error:
        assert "unregistered durable tool payload" in str(error)
    else:
        raise AssertionError("unregistered durable payload was serialized")


def test_legacy_receipt_codec_fails_closed() -> None:
    legacy = '{"codec":"tool-result+json@1","result":{}}'

    try:
        decode_tool_result_receipt(legacy, success=True)
    except ValueError as error:
        assert "unsupported tool result receipt codec" in str(error)
    else:
        raise AssertionError("legacy receipt codec was accepted")
