"""Request-scoped LLM attempt orchestration."""

from mote.runtime.models.failover.admission import (
    AdmissionPermit,
    AdmissionRejectedError,
    AdmissionResult,
    ResourceAdmissionController,
)
from mote.runtime.models.failover.availability import AvailabilityBreaker, AvailabilityPermit
from mote.runtime.models.failover.model_journal import (
    LocalModelCallJournal,
    ModelCallJournalError,
    ModelCallJournalIntegrityError,
    ModelCallJournalUnavailableError,
    default_model_call_journal_root,
)
from mote.runtime.models.failover.operator import (
    LocalModelOperatorAuditStore,
    OperatorAuditIntegrityError,
    OperatorAuditRequiredError,
    OperatorControlError,
    OperatorDrainIncompleteError,
    OperatorRevisionConflict,
)
from mote.runtime.models.failover.orchestrator import DEFAULT_MAX_WIRE_ATTEMPTS, AttemptOrchestrator, AttemptResumeSeed
from mote.runtime.models.failover.planner import FailoverPlanner
from mote.runtime.models.failover.policy import DefaultFailoverPolicy, FailoverPolicy, classify_failure
from mote.runtime.models.failover.runtime_state import AtomicModelRuntime, ModelRuntimeGeneration, ModelRuntimeLease
from mote.runtime.models.failover.snapshot import (
    ModelRuntimeSnapshot,
    RuntimeFailoverGroup,
    build_model_runtime_snapshot,
)
from mote.runtime.models.failover.transforms import CanonicalRequestTransformer

__all__ = [
    "AdmissionPermit",
    "AdmissionRejectedError",
    "AdmissionResult",
    "AvailabilityBreaker",
    "AvailabilityPermit",
    "DEFAULT_MAX_WIRE_ATTEMPTS",
    "AttemptOrchestrator",
    "AttemptResumeSeed",
    "AtomicModelRuntime",
    "CanonicalRequestTransformer",
    "DefaultFailoverPolicy",
    "FailoverPlanner",
    "FailoverPolicy",
    "LocalModelOperatorAuditStore",
    "LocalModelCallJournal",
    "ModelRuntimeSnapshot",
    "ModelRuntimeGeneration",
    "ModelRuntimeLease",
    "ModelCallJournalError",
    "ModelCallJournalIntegrityError",
    "ModelCallJournalUnavailableError",
    "OperatorAuditIntegrityError",
    "OperatorAuditRequiredError",
    "OperatorControlError",
    "OperatorDrainIncompleteError",
    "OperatorRevisionConflict",
    "ResourceAdmissionController",
    "RuntimeFailoverGroup",
    "build_model_runtime_snapshot",
    "classify_failure",
    "default_model_call_journal_root",
]
