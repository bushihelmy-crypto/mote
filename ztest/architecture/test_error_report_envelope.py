from __future__ import annotations

import json
from typing import get_type_hints

import pytest

from mote.contracts.events.application import ApplicationReadinessFailed
from mote.contracts.events.inference import ModelCacheDegraded
from mote.contracts.foundation.errors.codes import ErrorCode, RecoveryAction
from mote.contracts.foundation.errors.report import ErrorNamespace, ErrorReport
from mote.contracts.tool.errors import ToolError
from mote.orchestration.background_tasks.model import BackgroundTaskNotification
from mote.runtime.tools.tool_result import ToolResult
from mote.runtime.tools.tool_result_receipt import decode_tool_result_receipt, encode_tool_result_receipt


def _report() -> ErrorReport:
    return ErrorReport.from_exception(ToolError("failed"))


def test_strict_v1_round_trip_and_unknown_fields_fail_closed() -> None:
    wire = _report().as_dict()
    assert wire["schema"] == "mote.error-report/v1"
    assert wire["namespace"] == "tool"
    assert ErrorReport.from_dict(wire) == _report()
    for mutation in (
        {**wire, "schema": "mote.error-report/v0"},
        {**wire, "namespace": "unknown"},
        {**wire, "code": "OLD_TOOL"},
        {**wire, "extra": True},
        {**wire, "retryable": 1},
    ):
        with pytest.raises(ValueError):
            ErrorReport.from_dict(mutation)


def test_namespace_ownership_and_context_are_strict() -> None:
    with pytest.raises(ValueError, match="does not own"):
        ErrorReport(
            "ToolError",
            ErrorCode.TOOL.value,
            "failed",
            False,
            RecoveryAction.ABORT.value,
            namespace=ErrorNamespace.FILE,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"error": 1},
        {"message": object()},
        {"retryable": 1},
        {"cause": False},
    ],
)
def test_constructor_rejects_wrong_primitives(changes) -> None:
    values = {
        "error": "RuntimeError",
        "code": ErrorCode.UNKNOWN.value,
        "message": "failed",
        "retryable": False,
        "recovery": RecoveryAction.ABORT.value,
        "cause": None,
    }
    values.update(changes)
    with pytest.raises((TypeError, ValueError)):
        ErrorReport(**values)
    with pytest.raises(TypeError, match="JSON"):
        ErrorReport(
            "ToolError",
            ErrorCode.TOOL.value,
            "failed",
            False,
            RecoveryAction.ABORT.value,
            detail={"bad": object()},
        )


def test_tool_receipt_direct_cut_rejects_v2() -> None:
    payload = encode_tool_result_receipt(ToolResult(output="x", success=False, error=_report()))
    assert decode_tool_result_receipt(payload, success=False).error == _report()
    legacy = json.loads(payload)
    legacy["codec"] = "tool-result+json@2"
    with pytest.raises(ValueError, match="unsupported"):
        decode_tool_result_receipt(json.dumps(legacy), success=False)


def test_external_string_dtos_remain_negative_targets() -> None:
    assert get_type_hints(ApplicationReadinessFailed)["error_code"] is str
    assert get_type_hints(ModelCacheDegraded)["error_code"] is str


def test_session_notification_rejects_legacy_error_variant() -> None:
    message = BackgroundTaskNotification(content="failed", error=_report().as_dict())
    assert message.error == _report().as_dict()
    with pytest.raises(ValueError, match="shape"):
        BackgroundTaskNotification(content="failed", error={"message": "legacy"})
