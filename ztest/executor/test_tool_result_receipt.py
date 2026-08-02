from __future__ import annotations

import json

import pytest

from mote.contracts.artifact import ArtifactRetention, ArtifactSensitivity
from mote.contracts.async_work import DurableWorkflowRunReference, DurableWorkflowRunSubmission
from mote.contracts.foundation.errors.report import ErrorReport
from mote.contracts.tool.errors import ToolError
from mote.contracts.tool.result import ArtifactToolPayload, InlineBinaryToolPayload, json_tool_payload
from mote.contracts.workflow import WorkflowDefinitionId, WorkflowRunId, WorkflowRunReference
from mote.runtime.tools.tool_result import FileChange, ToolResult
from mote.runtime.tools.tool_result_receipt import decode_tool_result_receipt, encode_tool_result_receipt
from mote.ztest.artifact_fakes import artifact_media, artifact_ref


@pytest.mark.parametrize(
    "payload",
    [
        json_tool_payload({"nested": [1, "two", None]}),
        InlineBinaryToolPayload(b"raw"),
        ArtifactToolPayload(artifact_ref(b"payload", kind="payload")),
    ],
)
def test_each_durable_payload_variant_round_trips(payload) -> None:
    result = ToolResult(output="ok", payload=payload)

    restored = decode_tool_result_receipt(encode_tool_result_receipt(result), success=True)

    assert restored.payload == payload


def test_full_tool_result_receipt_round_trips_every_durable_field() -> None:
    media = artifact_media("image", b"image", ref="source.png")
    artifact = artifact_ref(b"report", kind="report")
    error = ErrorReport.from_exception(ToolError("structured failure"))
    result = ToolResult(
        output="final model output",
        success=False,
        payload=json_tool_payload({"tuple_is_canonical_array": [1, "two"]}),
        execution_value=object(),
        media=[media],
        artifacts=[artifact],
        file_changes=[
            FileChange(
                path="/workspace/file.txt",
                old="before",
                new="after",
                transaction_id="tx-1",
                post_digest="digest",
            )
        ],
        error=error,
        terminate=True,
        retention="pin",
        resource_path="/workspace/file.txt",
        async_work_submission=DurableWorkflowRunSubmission(
            DurableWorkflowRunReference(WorkflowRunReference(WorkflowRunId("run"), WorkflowDefinitionId("definition"))),
            3,
        ),
    )

    restored = decode_tool_result_receipt(encode_tool_result_receipt(result), success=False)

    assert restored.output == result.output
    assert restored.success is False
    assert restored.payload == result.payload
    assert restored.execution_value is None
    assert restored.media == result.media
    assert restored.artifacts == result.artifacts
    assert restored.file_changes == result.file_changes
    assert restored.error == result.error
    assert restored.terminate is True
    assert restored.retention == "pin"
    assert restored.resource_path == result.resource_path
    assert restored.async_work_submission == result.async_work_submission
    assert restored.media[0].artifact.retention is ArtifactRetention.SESSION
    assert restored.media[0].artifact.sensitivity is ArtifactSensitivity.PRIVATE


def test_unknown_payload_type_fails_at_construction_boundary() -> None:
    with pytest.raises(ValueError, match="canonical durable variant"):
        ToolResult(output="unsafe", payload=object())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda envelope: envelope.update(codec="tool-result+json@99"),
        lambda envelope: envelope["result"].update(extra=True),
        lambda envelope: envelope["result"].update(success=1),
        lambda envelope: envelope["result"].update(payload={"kind": "unknown"}),
    ],
)
def test_unknown_version_shape_and_primitives_fail_closed(mutation) -> None:
    envelope = json.loads(encode_tool_result_receipt(ToolResult(output="ok")))
    mutation(envelope)

    with pytest.raises(ValueError):
        decode_tool_result_receipt(json.dumps(envelope), success=True)


def test_legacy_or_non_json_receipt_is_not_interpreted_as_output() -> None:
    with pytest.raises(ValueError, match="valid JSON"):
        decode_tool_result_receipt("legacy output", success=True)
