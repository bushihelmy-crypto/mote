"""Single fenced coordinator for Agent-admitted durable Workflow creation."""

from mote.contracts.ports.runtime.operation_ownership import OperationOwnershipPort
from mote.contracts.ports.workflow.admission import WorkflowCreateAdmissionPort
from mote.contracts.runtime.errors import LeaseCoordinatorUnavailableError, LeaseUnavailableError
from mote.contracts.runtime.operation_ownership import EffectCapability, OperationBackend, OperationOwnershipRequest
from mote.contracts.workflow.admission import (
    ClaimWorkflowCreateAdmission,
    ReserveWorkflowCreateAdmission,
    SettleWorkflowCreateAdmission,
    WorkflowCreateAdmissionDisposition,
    WorkflowCreateAdmissionLifecycle,
)
from mote.contracts.workflow.authority import WorkflowCallerContext
from mote.orchestration.workflows.durable.control import WorkflowRunControl
from mote.orchestration.workflows.durable.model import CreateWorkflowRun, WorkflowRunProjection
from mote.orchestration.workflows.durable.store import WorkflowRunStore


class WorkflowCreateRejected(RuntimeError):
    def __init__(self, disposition: WorkflowCreateAdmissionDisposition) -> None:
        self.disposition = disposition
        super().__init__(f"Workflow create admission rejected: {disposition.value}")


class WorkflowCreateCoordinator:
    def __init__(
        self,
        control: WorkflowRunControl,
        admissions: WorkflowCreateAdmissionPort,
        ownership: OperationOwnershipPort,
        *,
        deployment_id: str,
        holder_id: str,
        backend: OperationBackend,
        lease_ttl_seconds: float = 30.0,
    ) -> None:
        self._control = control
        self._admissions = admissions
        self._ownership = ownership
        self._deployment_id = deployment_id
        self._holder_id = holder_id
        self._backend = backend
        self._lease_ttl_seconds = lease_ttl_seconds

    def create(self, command: CreateWorkflowRun, caller: WorkflowCallerContext) -> WorkflowRunProjection:
        provenance = command.provenance
        grant = command.access_grant
        if (
            provenance.creator_logical_agent_id != caller.logical_agent_id
            or provenance.creator_incarnation_generation != caller.incarnation_generation
            or provenance.creator_lineage_revision != caller.lineage_revision
            or provenance.creator_cancellation_epoch != caller.cancellation_epoch
            or provenance.root_governance_agent_id != caller.root_governance_agent_id
            or grant.authorized_logical_agent_id != caller.logical_agent_id
            or grant.root_governance_agent_id != caller.root_governance_agent_id
        ):
            raise ValueError("Workflow create authority facts do not bind the caller")
        admission_id = provenance.workflow_create_admission_id
        ownership = self._ownership.claim(
            OperationOwnershipRequest(
                self._deployment_id,
                str(admission_id),
                self._holder_id,
                self._backend,
                0,
                f"workflow-create:{admission_id}",
                EffectCapability.NO_EXTERNAL_EFFECT,
            ),
            self._lease_ttl_seconds,
        )
        try:
            reserved = self._admissions.reserve_workflow_create_admission(
                ReserveWorkflowCreateAdmission(
                    admission_id,
                    command.request_id,
                    command.reference,
                    caller,
                    provenance.creator_cancellation_epoch,
                    ownership,
                )
            )
            if (
                reserved.disposition
                not in {
                    WorkflowCreateAdmissionDisposition.RESERVED,
                    WorkflowCreateAdmissionDisposition.IDEMPOTENT,
                }
                or reserved.admission is None
            ):
                raise WorkflowCreateRejected(reserved.disposition)
            if reserved.admission.lifecycle is WorkflowCreateAdmissionLifecycle.ABORTED:
                raise WorkflowCreateRejected(WorkflowCreateAdmissionDisposition.PREVIOUS_ADMISSION_ABORTED)
            projection = self._control.create(command, admission_ownership=ownership)
            if reserved.admission.lifecycle is WorkflowCreateAdmissionLifecycle.COMMITTED:
                return projection
            settled = self._admissions.settle_workflow_create_admission(
                SettleWorkflowCreateAdmission(
                    admission_id,
                    WorkflowCreateAdmissionLifecycle.COMMITTED,
                    reserved.admission.revision,
                    ownership,
                )
            )
            if settled.disposition not in {
                WorkflowCreateAdmissionDisposition.SETTLED,
                WorkflowCreateAdmissionDisposition.IDEMPOTENT,
            }:
                raise WorkflowCreateRejected(settled.disposition)
            return projection
        finally:
            self._ownership.release(ownership)


class WorkflowCreateAdmissionReconciler:
    """Settles abandoned reservations without replaying Workflow creation."""

    def __init__(
        self,
        admissions: WorkflowCreateAdmissionPort,
        runs: WorkflowRunStore,
        ownership: OperationOwnershipPort,
        *,
        holder_id: str,
        lease_ttl_seconds: float = 30.0,
    ) -> None:
        self._admissions = admissions
        self._runs = runs
        self._ownership = ownership
        self._holder_id = holder_id
        self._lease_ttl_seconds = lease_ttl_seconds

    def reconcile(self) -> int:
        settled = 0
        for admission in self._admissions.reserved_workflow_create_admissions():
            request = admission.ownership.request
            try:
                ownership = self._ownership.claim(
                    OperationOwnershipRequest(
                        request.deployment_id,
                        request.operation_id,
                        self._holder_id,
                        request.backend,
                        admission.revision,
                        request.effect_id,
                        EffectCapability.NO_EXTERNAL_EFFECT,
                    ),
                    self._lease_ttl_seconds,
                )
            except (LeaseUnavailableError, LeaseCoordinatorUnavailableError):
                continue
            try:
                claimed = self._admissions.claim_workflow_create_admission(
                    ClaimWorkflowCreateAdmission(admission.admission_id, admission.revision, ownership)
                )
                if claimed.disposition is not WorkflowCreateAdmissionDisposition.CLAIMED or claimed.admission is None:
                    continue
                current = claimed.admission
                run = self._runs.get(current.reference)
                lifecycle = WorkflowCreateAdmissionLifecycle.ABORTED
                if run is not None:
                    self._validate_committed_run(current, run)
                    lifecycle = WorkflowCreateAdmissionLifecycle.COMMITTED
                receipt = self._admissions.settle_workflow_create_admission(
                    SettleWorkflowCreateAdmission(
                        current.admission_id,
                        lifecycle,
                        current.revision,
                        ownership,
                    )
                )
                if receipt.disposition in {
                    WorkflowCreateAdmissionDisposition.SETTLED,
                    WorkflowCreateAdmissionDisposition.IDEMPOTENT,
                }:
                    settled += 1
            finally:
                self._ownership.release(ownership)
        return settled

    @staticmethod
    def _validate_committed_run(admission, run: WorkflowRunProjection) -> None:
        provenance = run.provenance
        grant = run.access_grant
        if (
            run.request_id != admission.create_request_id
            or run.reference != admission.reference
            or provenance.workflow_create_admission_id != admission.admission_id
            or provenance.creator_logical_agent_id != admission.logical_agent_id
            or provenance.creator_lineage_revision != admission.lineage_revision
            or provenance.creator_cancellation_epoch != admission.cancellation_epoch
            or provenance.root_governance_agent_id != admission.root_agent_id
            or grant.authorized_logical_agent_id != admission.logical_agent_id
            or grant.root_governance_agent_id != admission.root_agent_id
        ):
            raise RuntimeError("reserved Workflow admission conflicts with committed run facts")


__all__ = [
    "WorkflowCreateAdmissionReconciler",
    "WorkflowCreateCoordinator",
    "WorkflowCreateRejected",
]
