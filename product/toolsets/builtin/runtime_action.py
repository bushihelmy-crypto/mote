"""Shared implementations for model-facing stateful Runtime actions."""

from __future__ import annotations

from mote.contracts.authorization import PermissionDecision
from mote.contracts.interaction.handoff import HandoffStatus
from mote.contracts.tool.result import json_tool_payload
from mote.runtime.tools.capability_types import HandoffRuntime
from mote.runtime.tools.tool_result import ToolResult


def handoff_permission() -> PermissionDecision:
    return PermissionDecision.allow("safe", "handoff is an explicit human-interaction boundary")


def is_handoff_action(args: dict) -> bool:
    return str(args.get("action") or "").strip().lower() == "handoff"


async def run_handoff_action(
    handoff_runtime: HandoffRuntime,
    runtime: str,
    *,
    message: str = "",
) -> ToolResult:
    """Transfer an existing Runtime and normalize the human outcome."""
    outcome = await handoff_runtime(runtime, message=message)
    status = outcome.status
    if status is HandoffStatus.COMPLETED:
        output = f"User completed handoff of {outcome.runtime_ref.readable}."
    elif status is HandoffStatus.CANCELLED:
        output = f"User cancelled handoff of {outcome.runtime_ref.readable}."
    elif status is HandoffStatus.UNAVAILABLE:
        output = f"Interactive handoff is unavailable for {outcome.runtime_ref.readable}."
    else:
        output = f"Handoff of {outcome.runtime_ref.readable} ended with status: " f"{status.value}."
    if outcome.human_message:
        output += f" User message: {outcome.human_message}"
    if outcome.detail:
        output += f" Detail: {outcome.detail}"
    if outcome.summary:
        output += f" {outcome.summary}"
    if outcome.resume_hint:
        output += f" Resume hint: {outcome.resume_hint}"
    return ToolResult(
        output=output,
        success=status not in {HandoffStatus.FAILED, HandoffStatus.UNAVAILABLE},
        payload=json_tool_payload(
            {
                "status": outcome.status.value,
                "runtime": {
                    "runtime_id": outcome.runtime_ref.runtime_id,
                    "kind": outcome.runtime_ref.kind,
                    "alias": outcome.runtime_ref.alias,
                },
                "from_revision": outcome.from_revision,
                "to_revision": outcome.to_revision,
                "human_message": outcome.human_message,
                "detail": outcome.detail,
                "summary": outcome.summary,
                "resume_hint": outcome.resume_hint,
            }
        ),
    )


__all__ = ["handoff_permission", "is_handoff_action", "run_handoff_action"]
