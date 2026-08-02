from __future__ import annotations

import pytest

from mote.contracts.agent.runtime_identity import AgentId, CancellationEpoch, LineageRevision
from mote.contracts.workflow import (
    WorkflowCreateAdmissionId,
    WorkflowGovernanceCancelRequest,
    decode_workflow_governance_cancel,
    encode_workflow_governance_cancel,
)


def _request() -> WorkflowGovernanceCancelRequest:
    root = AgentId("root")
    subtree = AgentId("child")
    epoch = CancellationEpoch(3)
    return WorkflowGovernanceCancelRequest(
        WorkflowGovernanceCancelRequest.derive_id(root, subtree, epoch),
        root,
        subtree,
        LineageRevision(9),
        epoch,
        (AgentId("child"), AgentId("grandchild")),
        (WorkflowCreateAdmissionId("admission-1"),),
    )


def test_governance_cancel_codec_preserves_frozen_cutoff() -> None:
    request = _request()
    assert decode_workflow_governance_cancel(encode_workflow_governance_cancel(request)) == request


@pytest.mark.parametrize("mutation", ["extra", "wrong_epoch", "duplicate_target"])
def test_governance_cancel_codec_fails_closed(mutation: str) -> None:
    payload = encode_workflow_governance_cancel(_request())
    if mutation == "extra":
        payload["extra"] = True
    elif mutation == "wrong_epoch":
        payload["cancellation_epoch"] = "3"
    else:
        payload["target_agent_ids"] = ["child", "child"]
    with pytest.raises((TypeError, ValueError)):
        decode_workflow_governance_cancel(payload)
