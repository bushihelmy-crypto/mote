"""Durable audit boundary for model endpoint operator controls."""

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from mote.contracts.model.failover import OperatorState, OperatorStatus, OperatorTransition, ResourceIdentity


class ModelOperatorAuditStore(Protocol):
    def append(self, transition: OperatorTransition) -> None:
        ...

    def records(self) -> Sequence[OperatorTransition]:
        ...


@runtime_checkable
class ModelOperatorControl(Protocol):
    def transition_operator_state(
        self,
        resource: ResourceIdentity,
        state: OperatorState,
        *,
        expected_revision: int,
        config_revision: str,
        actor: str,
        reason: str,
        force: bool = False,
    ) -> OperatorTransition:
        ...

    def operator_status(self, resource: ResourceIdentity) -> OperatorStatus:
        ...

    async def wait_drained(
        self,
        resource: ResourceIdentity,
        *,
        timeout_seconds: float | None = None,
    ) -> OperatorStatus:
        ...


__all__ = ["ModelOperatorAuditStore", "ModelOperatorControl"]
