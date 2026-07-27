from __future__ import annotations

from mote.contracts.artifacts import ArtifactRetention, ArtifactSensitivity
from mote.runtime.errors import ErrorReport, ToolError
from mote.runtime.tools.tool_result import FileChange, ToolResult
from mote.runtime.tools.tool_result_receipt import decode_tool_result_receipt, encode_tool_result_receipt
from mote.ztest.artifact_fakes import artifact_media, artifact_ref


def test_full_tool_result_receipt_round_trips_every_durable_field():
    media = artifact_media("image", b"image", ref="source.png")
    artifact = artifact_ref(b"report", kind="report")
    error = ErrorReport.from_exception(ToolError("structured failure"))
    result = ToolResult(
        output="final model output",
        success=False,
        data={"bytes": b"raw", "tuple": (1, "two")},
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
    )

    restored = decode_tool_result_receipt(
        encode_tool_result_receipt(result),
        success=True,
    )

    assert restored.output == result.output
    assert restored.success is False
    assert restored.data == result.data
    assert restored.media == result.media
    assert restored.artifacts == result.artifacts
    assert restored.file_changes == result.file_changes
    assert restored.error == result.error
    assert restored.terminate is True
    assert restored.retention == "pin"
    assert restored.resource_path == result.resource_path
    assert restored.media[0].artifact.retention is ArtifactRetention.SESSION
    assert restored.media[0].artifact.sensitivity is ArtifactSensitivity.PRIVATE


def test_legacy_output_only_receipt_remains_readable():
    restored = decode_tool_result_receipt("legacy output", success=True)

    assert restored == ToolResult(output="legacy output", success=True)
