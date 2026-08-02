"""Authority-bound projection of canonical durable Workflow facts."""

from mote.contracts.async_work.command import (
    CancelDurableWorkflowRun,
    ResumeDurableWorkflowRun,
    WorkflowCancelDisposition,
    WorkflowCancelReceipt,
    WorkflowResumeDisposition,
    WorkflowResumeReceipt,
)
from mote.contracts.async_work.identity import DurableWorkflowRunReference
from mote.contracts.async_work.observation import (
    AsyncWorkAction,
    AsyncWorkPresentationPhase,
    DurableWorkflowObservationDetail,
    DurableWorkflowRunObservation,
    WorkflowPauseDetail,
    WorkflowPausePresentationReason,
    WorkflowTerminalDeliveryObservation,
    WorkflowTerminalDeliveryState,
)
from mote.contracts.ports.async_work.observation import AsyncWorkQueryDisposition, AsyncWorkQueryResult
from mote.contracts.ports.workflow.authority import WorkflowCallerAuthorizationPort
from mote.contracts.ports.workflow.governance import WorkflowGovernanceAdmissionQueryPort
from mote.contracts.runtime.errors import LeaseCoordinatorUnavailableError, LeaseFencedError, LeaseUnavailableError
from mote.contracts.runtime.operation_ownership import OperationOwnership
from mote.contracts.workflow.admission import WorkflowCreateAdmission, WorkflowCreateAdmissionLifecycle
from mote.contracts.workflow.authority import WorkflowCallerAuthorizationDisposition, WorkflowCallerContext
from mote.orchestration.workflows.durable.control import WorkflowRunControl
from mote.orchestration.workflows.durable.model import (
    ResumeWorkflowRun,
    WorkflowRunCommand,
    WorkflowRunPhase,
    WorkflowRunProjection,
)
from mote.orchestration.workflows.durable.reconciliation import WorkflowReconciliationStore
from mote.orchestration.workflows.durable.store import WorkflowRunStore

_PHASES = {
    WorkflowRunPhase.CREATED: AsyncWorkPresentationPhase.QUEUED,
    WorkflowRunPhase.RUNNING: AsyncWorkPresentationPhase.RUNNING,
    WorkflowRunPhase.PAUSED: AsyncWorkPresentationPhase.PAUSED,
    WorkflowRunPhase.CANCELLING: AsyncWorkPresentationPhase.CANCELLING,
    WorkflowRunPhase.CANCELLED: AsyncWorkPresentationPhase.CANCELLED,
    WorkflowRunPhase.SUCCEEDED: AsyncWorkPresentationPhase.SUCCEEDED,
    WorkflowRunPhase.FAILED: AsyncWorkPresentationPhase.FAILED,
    WorkflowRunPhase.TIMED_OUT: AsyncWorkPresentationPhase.TIMED_OUT,
}


class BoundWorkflowAsyncWorkAdapter:
    def __init__(
        self,
        caller: WorkflowCallerContext,
        authorizer: WorkflowCallerAuthorizationPort,
        admissions: WorkflowGovernanceAdmissionQueryPort,
        runs: WorkflowRunStore,
        reconciliation: WorkflowReconciliationStore,
        control: WorkflowRunControl,
        execution_ownership: OperationOwnership | None = None,
    ) -> None:
        self._caller = caller
        self._authorizer = authorizer
        self._admissions = admissions
        self._runs = runs
        self._reconciliation = reconciliation
        self._control = control
        self._execution_ownership = execution_ownership

    def get(self, reference: DurableWorkflowRunReference) -> AsyncWorkQueryResult:
        authority = self._authority_disposition()
        if authority is not None:
            return AsyncWorkQueryResult(authority, None)
        try:
            run = self._runs.get(reference.reference)
        except RuntimeError:
            return AsyncWorkQueryResult(AsyncWorkQueryDisposition.DEFINITION_MISMATCH, None)
        if run is None:
            return AsyncWorkQueryResult(AsyncWorkQueryDisposition.NOT_FOUND, None)
        if not self._admission_matches(run):
            return AsyncWorkQueryResult(AsyncWorkQueryDisposition.CONTROL_UNAVAILABLE, None)
        if (
            run.access_grant.authorized_logical_agent_id != self._caller.logical_agent_id
            or run.access_grant.root_governance_agent_id != self._caller.root_governance_agent_id
        ):
            return AsyncWorkQueryResult(AsyncWorkQueryDisposition.PRINCIPAL_MISMATCH, None)
        actions: tuple[AsyncWorkAction, ...]
        if run.phase in {WorkflowRunPhase.CREATED, WorkflowRunPhase.RUNNING}:
            actions = (AsyncWorkAction.CANCEL,)
        elif run.phase is WorkflowRunPhase.PAUSED:
            actions = (AsyncWorkAction.RESUME, AsyncWorkAction.CANCEL)
        elif run.phase.terminal:
            actions = (AsyncWorkAction.VIEW_RESULT,)
        else:
            actions = ()
        pause = None
        if run.pause_reason is not None:
            pause = WorkflowPauseDetail(
                WorkflowPausePresentationReason(run.pause_reason.value),
                run.resume_nonce,
            )
        deliveries = tuple(
            WorkflowTerminalDeliveryObservation(
                item.delivery_id,
                item.destination_id,
                item.revision,
                WorkflowTerminalDeliveryState(item.state.value),
                item.attempts,
                item.next_eligible_at,
                item.reason or None,
            )
            for item in self._reconciliation.terminal_deliveries_for_run(run.reference.run_id)
        )
        observation = DurableWorkflowRunObservation(
            reference,
            run.revision,
            _PHASES[run.phase],
            DurableWorkflowObservationDetail(pause),
            run.frontier,
            run.deadline,
            run.terminal_result,
            actions,
            deliveries,
        )
        return AsyncWorkQueryResult(AsyncWorkQueryDisposition.FOUND, observation)

    def cancel(self, command: CancelDurableWorkflowRun) -> WorkflowCancelReceipt:
        authorization = self._authorizer.authorize_workflow_caller(self._caller)
        if authorization.disposition is not WorkflowCallerAuthorizationDisposition.AUTHORIZED:
            disposition = {
                WorkflowCallerAuthorizationDisposition.NOT_FOUND: WorkflowCancelDisposition.CALLER_NOT_ACTIVE,
                WorkflowCallerAuthorizationDisposition.NOT_ACTIVE: WorkflowCancelDisposition.CALLER_NOT_ACTIVE,
                WorkflowCallerAuthorizationDisposition.INCARNATION_MISMATCH: WorkflowCancelDisposition.INCARNATION_MISMATCH,
                WorkflowCallerAuthorizationDisposition.ROOT_MISMATCH: WorkflowCancelDisposition.PRINCIPAL_MISMATCH,
                WorkflowCallerAuthorizationDisposition.STALE_FENCE: WorkflowCancelDisposition.FENCE_LOST,
            }[authorization.disposition]
            return WorkflowCancelReceipt(command.reference, disposition, None)
        if authorization.lineage_revision != self._caller.lineage_revision:
            return WorkflowCancelReceipt(
                command.reference,
                WorkflowCancelDisposition.LINEAGE_REVISION_STALE,
                None,
            )
        try:
            run = self._runs.get(command.reference.reference)
        except RuntimeError:
            return WorkflowCancelReceipt(command.reference, WorkflowCancelDisposition.DEFINITION_MISMATCH, None)
        if run is None:
            return WorkflowCancelReceipt(command.reference, WorkflowCancelDisposition.NOT_FOUND, None)
        if not self._admission_matches(run):
            return WorkflowCancelReceipt(
                command.reference,
                WorkflowCancelDisposition.CONTROL_UNAVAILABLE,
                run.revision,
            )
        if (
            run.access_grant.authorized_logical_agent_id != self._caller.logical_agent_id
            or run.access_grant.root_governance_agent_id != self._caller.root_governance_agent_id
        ):
            return WorkflowCancelReceipt(
                command.reference,
                WorkflowCancelDisposition.PRINCIPAL_MISMATCH,
                run.revision,
            )
        if run.revision != command.expected_revision:
            return WorkflowCancelReceipt(
                command.reference,
                WorkflowCancelDisposition.REVISION_CONFLICT,
                run.revision,
            )
        if run.phase.terminal:
            disposition = WorkflowCancelDisposition.ALREADY_TERMINAL
        elif run.phase is WorkflowRunPhase.CANCELLING:
            disposition = WorkflowCancelDisposition.ALREADY_CANCELLING
        else:
            try:
                updated = self._control.cancel(WorkflowRunCommand(run.reference, run.revision))
            except LeaseUnavailableError:
                return WorkflowCancelReceipt(
                    command.reference,
                    WorkflowCancelDisposition.CLAIM_CONFLICT,
                    run.revision,
                )
            except LeaseFencedError:
                return WorkflowCancelReceipt(
                    command.reference,
                    WorkflowCancelDisposition.FENCE_LOST,
                    run.revision,
                )
            except LeaseCoordinatorUnavailableError:
                return WorkflowCancelReceipt(
                    command.reference,
                    WorkflowCancelDisposition.CONTROL_UNAVAILABLE,
                    run.revision,
                )
            except RuntimeError:
                return WorkflowCancelReceipt(
                    command.reference,
                    WorkflowCancelDisposition.REVISION_CONFLICT,
                    run.revision,
                )
            disposition = WorkflowCancelDisposition.CANCEL_REQUESTED
            run = updated
        return WorkflowCancelReceipt(command.reference, disposition, run.revision)

    def _authority_disposition(self) -> AsyncWorkQueryDisposition | None:
        receipt = self._authorizer.authorize_workflow_caller(self._caller)
        if (
            receipt.disposition is WorkflowCallerAuthorizationDisposition.AUTHORIZED
            and receipt.lineage_revision != self._caller.lineage_revision
        ):
            return AsyncWorkQueryDisposition.CONTROL_UNAVAILABLE
        return {
            WorkflowCallerAuthorizationDisposition.AUTHORIZED: None,
            WorkflowCallerAuthorizationDisposition.NOT_FOUND: AsyncWorkQueryDisposition.CONTROL_UNAVAILABLE,
            WorkflowCallerAuthorizationDisposition.NOT_ACTIVE: AsyncWorkQueryDisposition.CONTROL_UNAVAILABLE,
            WorkflowCallerAuthorizationDisposition.INCARNATION_MISMATCH: AsyncWorkQueryDisposition.CONTROL_UNAVAILABLE,
            WorkflowCallerAuthorizationDisposition.ROOT_MISMATCH: AsyncWorkQueryDisposition.PRINCIPAL_MISMATCH,
            WorkflowCallerAuthorizationDisposition.STALE_FENCE: AsyncWorkQueryDisposition.CONTROL_UNAVAILABLE,
        }[receipt.disposition]

    def _admission_matches(self, run: WorkflowRunProjection) -> bool:
        admission = self._admissions.get_workflow_create_admission(run.provenance.workflow_create_admission_id)
        return (
            isinstance(admission, WorkflowCreateAdmission)
            and admission.lifecycle is WorkflowCreateAdmissionLifecycle.COMMITTED
            and admission.create_request_id == run.request_id
            and admission.reference == run.reference
            and admission.logical_agent_id == run.provenance.creator_logical_agent_id
            and admission.root_agent_id == run.provenance.root_governance_agent_id
            and admission.lineage_revision == run.provenance.creator_lineage_revision
            and admission.cancellation_epoch == run.provenance.creator_cancellation_epoch
        )

    def resume(self, command: ResumeDurableWorkflowRun) -> WorkflowResumeReceipt:
        authorization = self._authorizer.authorize_workflow_caller(self._caller)
        if authorization.disposition is not WorkflowCallerAuthorizationDisposition.AUTHORIZED:
            disposition = {
                WorkflowCallerAuthorizationDisposition.NOT_FOUND: WorkflowResumeDisposition.CALLER_NOT_ACTIVE,
                WorkflowCallerAuthorizationDisposition.NOT_ACTIVE: WorkflowResumeDisposition.CALLER_NOT_ACTIVE,
                WorkflowCallerAuthorizationDisposition.INCARNATION_MISMATCH: WorkflowResumeDisposition.INCARNATION_MISMATCH,
                WorkflowCallerAuthorizationDisposition.ROOT_MISMATCH: WorkflowResumeDisposition.PRINCIPAL_MISMATCH,
                WorkflowCallerAuthorizationDisposition.STALE_FENCE: WorkflowResumeDisposition.FENCE_LOST,
            }[authorization.disposition]
            return WorkflowResumeReceipt(command.reference, disposition, None)
        if authorization.lineage_revision != self._caller.lineage_revision:
            return WorkflowResumeReceipt(
                command.reference,
                WorkflowResumeDisposition.LINEAGE_REVISION_STALE,
                None,
            )
        try:
            run = self._runs.get(command.reference.reference)
        except RuntimeError:
            return WorkflowResumeReceipt(
                command.reference,
                WorkflowResumeDisposition.DEFINITION_MISMATCH,
                None,
            )
        if run is None:
            return WorkflowResumeReceipt(command.reference, WorkflowResumeDisposition.NOT_FOUND, None)
        if not self._admission_matches(run):
            return WorkflowResumeReceipt(
                command.reference,
                WorkflowResumeDisposition.CONTROL_UNAVAILABLE,
                run.revision,
            )
        if (
            run.access_grant.authorized_logical_agent_id != self._caller.logical_agent_id
            or run.access_grant.root_governance_agent_id != self._caller.root_governance_agent_id
        ):
            return WorkflowResumeReceipt(
                command.reference,
                WorkflowResumeDisposition.PRINCIPAL_MISMATCH,
                run.revision,
            )
        if run.revision != command.expected_revision:
            return WorkflowResumeReceipt(
                command.reference,
                WorkflowResumeDisposition.REVISION_CONFLICT,
                run.revision,
            )
        if run.phase is not WorkflowRunPhase.PAUSED:
            return WorkflowResumeReceipt(
                command.reference,
                WorkflowResumeDisposition.NOT_PAUSED,
                run.revision,
            )
        try:
            updated = self._control.resume(
                ResumeWorkflowRun(
                    run.reference,
                    run.revision,
                    command.resume_nonce,
                    command.checkpoint_payload,
                    command.frontier,
                ),
                execution_ownership=self._execution_ownership,
            )
        except LeaseUnavailableError:
            disposition = WorkflowResumeDisposition.CLAIM_CONFLICT
        except LeaseFencedError:
            disposition = WorkflowResumeDisposition.FENCE_LOST
        except LeaseCoordinatorUnavailableError:
            disposition = WorkflowResumeDisposition.CONTROL_UNAVAILABLE
        except RuntimeError:
            disposition = WorkflowResumeDisposition.REVISION_CONFLICT
        else:
            return WorkflowResumeReceipt(
                command.reference,
                WorkflowResumeDisposition.RESUMED,
                updated.revision,
            )
        return WorkflowResumeReceipt(command.reference, disposition, run.revision)


__all__ = ["BoundWorkflowAsyncWorkAdapter"]
