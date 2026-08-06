from __future__ import annotations

import dataclasses

import pytest

from mote.contracts.execution.pending_act import (
    PendingActFrontier,
    PendingActFrontierId,
    PendingAction,
    PendingActionArgumentsRevision,
    ToolCompositionDefinitionRef,
)
from mote.contracts.execution.run_cursor import RecoveryTarget, RunRecoveryCursor
from mote.contracts.interaction import ApprovalState
from mote.contracts.tool import ExternalEffectState, ToolEffect, ToolInvocationId, tool_arguments_digest


def _definition() -> ToolCompositionDefinitionRef:
    return ToolCompositionDefinitionRef(
        blueprint_identity="coding-agent",
        blueprint_version="1",
        executable_digest="sha256-executable",
        composition_generation_id="generation-1",
        catalog_fingerprint="sha256-catalog",
        provider_descriptor_digest="sha256-provider",
        policy_generation="policy-1",
        capability_fingerprint="sha256-capability",
    )


def _action(ordinal: int = 0) -> PendingAction:
    return PendingAction(
        ordinal=ordinal,
        invocation_id=ToolInvocationId(f"invocation-{ordinal}"),
        action_id=f"action-{ordinal}",
        tool_name="Read",
        definition_identity="read/v1",
        catalog_generation=1,
        effect=ToolEffect.PURE,
        current_arguments_revision=0,
    )


def test_pending_act_contains_links_but_no_mixed_lifecycle_state() -> None:
    frontier = PendingActFrontier(
        schema_version=1,
        frontier_id=PendingActFrontierId("frontier-1"),
        session_id="session-1",
        run_id="run-1",
        model_call_id="model-call-1",
        revision=0,
        definition_ref=_definition(),
        actions=(_action(),),
    )

    assert frontier.actions[0].effect is ToolEffect.PURE
    assert "state" not in {field.name for field in dataclasses.fields(PendingAction)}
    assert "in_doubt" not in {state.value for state in ApprovalState}
    assert ExternalEffectState.IN_DOUBT.value == "in_doubt"


def test_argument_revision_is_immutable_and_digest_checked() -> None:
    arguments = {"path": "README.md"}
    revision = PendingActionArgumentsRevision(
        ToolInvocationId("invocation-1"),
        0,
        arguments,
        tool_arguments_digest(arguments),
    )
    arguments["path"] = "changed"

    assert revision.arguments["path"] == "README.md"
    with pytest.raises(ValueError, match="digest"):
        PendingActionArgumentsRevision(
            ToolInvocationId("invocation-1"),
            1,
            {"path": "other"},
            revision.arguments_digest,
        )


def test_frontier_requires_contiguous_unique_actions() -> None:
    with pytest.raises(ValueError, match="contiguous"):
        PendingActFrontier(
            1,
            PendingActFrontierId("frontier-1"),
            "session-1",
            "run-1",
            "model-call-1",
            0,
            _definition(),
            (_action(1),),
        )


def test_cursor_act_requires_pending_frontier_identity() -> None:
    with pytest.raises(ValueError, match="active PendingAct"):
        RunRecoveryCursor("run-1", 0, RecoveryTarget.ACT, None, False)


def test_local_action_requires_a_stable_fileops_transaction_identity() -> None:
    with pytest.raises(ValueError, match="FileOps transaction"):
        PendingAction(
            ordinal=0,
            invocation_id=ToolInvocationId("invocation-local"),
            action_id="action-local",
            tool_name="Edit",
            definition_identity="edit/v1",
            catalog_generation=1,
            effect=ToolEffect.LOCAL,
            current_arguments_revision=0,
        )
