from __future__ import annotations

import pytest

from mote.contracts.artifact import ArtifactRef
from mote.contracts.workflow import (
    WorkflowRunId,
    WorkflowSucceededArtifact,
    WorkflowSucceededInline,
    WorkflowTerminalResult,
    decode_workflow_terminal_result,
    encode_workflow_terminal_result,
)
from mote.contracts.workflow.result import MAX_WORKFLOW_INLINE_RESULT_BYTES


def _artifact() -> ArtifactRef:
    return ArtifactRef(
        artifact_id="workflow-result",
        revision=1,
        representation="canonical",
        kind="workflow-result",
        mime_type="application/json",
        content_ref="cas:workflow-result",
        digest="a" * 64,
        size=100_000,
    )


def test_large_workflow_terminal_result_uses_artifact_ref() -> None:
    result = WorkflowTerminalResult(
        WorkflowRunId("run"),
        4,
        WorkflowSucceededArtifact(_artifact()),
    )
    assert decode_workflow_terminal_result(encode_workflow_terminal_result(result)) == result


def test_workflow_inline_terminal_result_is_bounded() -> None:
    WorkflowSucceededInline("x" * MAX_WORKFLOW_INLINE_RESULT_BYTES)
    with pytest.raises(ValueError, match="byte limit"):
        WorkflowSucceededInline("x" * (MAX_WORKFLOW_INLINE_RESULT_BYTES + 1))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update(kind="paused"),
        lambda payload: payload.update(kind="in_doubt"),
        lambda payload: payload["payload"].update(extra=True),
        lambda payload: payload.update(terminal_revision=True),
    ],
)
def test_workflow_terminal_codec_rejects_nonterminal_and_malformed_variants(
    mutation,
) -> None:
    payload = encode_workflow_terminal_result(
        WorkflowTerminalResult(WorkflowRunId("run"), 2, WorkflowSucceededInline("done"))
    )
    mutation(payload)
    with pytest.raises((TypeError, ValueError)):
        decode_workflow_terminal_result(payload)
