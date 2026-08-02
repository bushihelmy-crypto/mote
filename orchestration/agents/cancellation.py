"""Supervisor-owned subtree cancellation orchestration."""

from __future__ import annotations

import asyncio

from mote.contracts.agent.cancellation import (
    AgentCancellationCommand,
    AgentCancellationDisposition,
    AgentCancellationPort,
    AgentCancellationReceipt,
    SubtreeCancellationReceipt,
)
from mote.contracts.agent.runtime_identity import AgentId, CancellationEpoch, LineageRevision
from mote.contracts.ports.runtime.lease import LeaseEpoch
from mote.contracts.ports.workflow.governance import WorkflowGovernanceCancellationDeliveryPort
from mote.contracts.workflow.authority import WorkflowCreateAdmissionId
from mote.contracts.workflow.governance import WorkflowGovernanceCancelRequest
from mote.orchestration.agents.lineage.store import AgentLineageStore


class SubtreeCancellationCoordinator:
    """Coordinates commands without reading any Agent-owned task/runtime maps."""

    def __init__(
        self,
        lineage: AgentLineageStore,
        dispatcher: AgentCancellationPort,
        workflow_delivery: WorkflowGovernanceCancellationDeliveryPort | None = None,
    ) -> None:
        self._lineage = lineage
        self._dispatcher = dispatcher
        self._workflow_delivery = workflow_delivery

    async def cancel(
        self,
        subtree_agent_id: str,
        *,
        lease: LeaseEpoch,
        timeout_seconds: float,
        cancellation_epoch: int | None = None,
    ) -> SubtreeCancellationReceipt:
        snapshot = (
            self._lineage.begin_subtree_cancellation(subtree_agent_id, lease=lease)
            if cancellation_epoch is None
            else self._lineage.cancellation_snapshot(subtree_agent_id, cancellation_epoch=cancellation_epoch)
        )
        if self._workflow_delivery is not None:
            root_id = AgentId(snapshot.root_agent_id)
            subtree_id = AgentId(snapshot.subtree_agent_id)
            epoch = CancellationEpoch(snapshot.cancellation_epoch)
            self._workflow_delivery.submit(
                WorkflowGovernanceCancelRequest(
                    WorkflowGovernanceCancelRequest.derive_id(root_id, subtree_id, epoch),
                    root_id,
                    subtree_id,
                    LineageRevision(snapshot.revision),
                    epoch,
                    tuple(AgentId(value) for value in snapshot.agent_ids),
                    tuple(WorkflowCreateAdmissionId(value) for value in snapshot.workflow_create_admission_ids),
                )
            )

        async def dispatch(agent_id: str) -> AgentCancellationReceipt:
            command = AgentCancellationCommand(
                snapshot.root_agent_id,
                snapshot.subtree_agent_id,
                agent_id,
                snapshot.revision,
                snapshot.cancellation_epoch,
            )
            try:
                return await asyncio.wait_for(
                    self._dispatcher.cancel_agent_scope(command),
                    timeout=timeout_seconds,
                )
            except TimeoutError:
                return AgentCancellationReceipt(
                    agent_id,
                    snapshot.cancellation_epoch,
                    AgentCancellationDisposition.TIMEOUT,
                )

        settlements = await asyncio.gather(*(dispatch(agent_id) for agent_id in snapshot.agent_ids))
        return SubtreeCancellationReceipt(
            snapshot.root_agent_id,
            snapshot.subtree_agent_id,
            snapshot.revision,
            snapshot.cancellation_epoch,
            tuple(settlements),
        )


__all__ = ["SubtreeCancellationCoordinator"]
