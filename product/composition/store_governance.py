"""Active durable-store authority and zero migration-debt declarations."""

from mote.contracts.events.governance import (
    ActiveStoreDeclaration,
    ArchiveCapabilityDeclaration,
    RestoreSourceClassification,
    RestoreSourceDisposition,
)
from mote.product.inference.backends.sqlite import (
    INFERENCE_GATEWAY_CUTOVER_UNIT,
    INFERENCE_GATEWAY_LOGICAL_STORE,
    INFERENCE_GATEWAY_STORAGE_FORMAT_VERSION,
    INFERENCE_GATEWAY_STORE_GENERATION,
)
from mote.product.inference.daemon.operations_audit_codec import OPERATIONS_AUDIT_ACTIVE_CODEC
from mote.runtime.session.codec import SESSION_ACTIVE_CODECS

ACTIVE_STORE_DECLARATIONS = (
    ActiveStoreDeclaration(
        cutover_unit_id="session-rollout-v1",
        logical_store="session-rollout",
        active_generation=1,
        included_event_families=tuple(entry.event_family for entry in SESSION_ACTIVE_CODECS),
        storage_format_version=1,
        canonical_reader="mote.runtime.session.log.SessionLog.iter_events",
        canonical_writer="mote.runtime.session.committer.SessionFactCommitter",
        activation_authority="mote.runtime.events.journal.LocalEventJournal",
        restore_admission="session streams are admitted only by verified canonical journal replay",
    ),
    ActiveStoreDeclaration(
        cutover_unit_id="inference-operations-audit-v1",
        logical_store="inference-operations-audit",
        active_generation=OPERATIONS_AUDIT_ACTIVE_CODEC.store_generation,
        included_event_families=(OPERATIONS_AUDIT_ACTIVE_CODEC.event_family,),
        storage_format_version=1,
        canonical_reader="mote.product.inference.daemon.operations_audit.SharedOperationsAudit.read",
        canonical_writer="mote.product.inference.daemon.operations_audit.SharedOperationsAudit.record",
        activation_authority="mote.runtime.events.journal.LocalEventJournal",
        restore_admission="audit streams are retained with their daemon authority and admitted only by verified canonical journal replay",
    ),
    ActiveStoreDeclaration(
        cutover_unit_id=INFERENCE_GATEWAY_CUTOVER_UNIT,
        logical_store=INFERENCE_GATEWAY_LOGICAL_STORE,
        active_generation=INFERENCE_GATEWAY_STORE_GENERATION,
        included_event_families=("inference-gateway-authority",),
        storage_format_version=INFERENCE_GATEWAY_STORAGE_FORMAT_VERSION,
        canonical_reader="mote.product.inference.backends.sqlite.SQLiteAttemptReceiptStore",
        canonical_writer="mote.product.inference.daemon.application.SharedDaemonApplication",
        activation_authority="mote.product.inference.backends.sqlite.SQLiteAttemptReceiptStore.activate_generation",
        restore_admission="mote.product.inference.restore.IsolatedSQLiteRestoreService.apply",
    ),
)

# Final state: migration declarations describe only live, removable debt. Keeping
# retired tombstones here would itself retain migration authority in production.
MIGRATION_DEBT_DECLARATIONS = ()
RESTORE_COPY_DECLARATIONS = ()
RESTORE_SOURCE_CLASSIFICATIONS = (
    RestoreSourceClassification(
        source_id="inference-sqlite-online-backup",
        source_symbol="mote.product.inference.backends.sqlite.SQLiteAttemptReceiptStore.backup_to",
        disposition=RestoreSourceDisposition.RESTORE_CAPABLE,
        logical_store="inference-gateway-authority",
        admission_contract="mote.product.inference.restore.IsolatedSQLiteRestoreService.apply",
        metadata_authority="mote.product.inference.backends.sqlite.SQLiteAttemptReceiptStore.describe_backup",
        lifecycle_policy="operator retention and legal hold; isolated restore only",
    ),
    RestoreSourceClassification(
        source_id="inference-sqlite-corrupt-image",
        source_symbol="mote.product.inference.backends.sqlite.SQLiteAttemptReceiptStore.preserve_corrupt_copy",
        disposition=RestoreSourceDisposition.EVIDENCE_ONLY,
        logical_store="inference-gateway-authority",
        admission_contract="never admitted directly; forensic evidence only",
        metadata_authority="startup integrity failure plus byte identity",
        lifecycle_policy="operator incident retention and destruction",
    ),
    RestoreSourceClassification(
        source_id="event-subscription-dead-letter",
        source_symbol="mote.runtime.events.backends.subscription_state.SQLiteSubscriptionStateStore.quarantine",
        disposition=RestoreSourceDisposition.EVIDENCE_ONLY,
        logical_store="subscription-state",
        admission_contract="never mounted as a journal; replay requires domain re-admission",
        metadata_authority="mote.contracts.ports.events.subscription.DeadLetterEntry",
        lifecycle_policy="session directory retention and legal hold",
    ),
    RestoreSourceClassification(
        source_id="session-export-and-artifact-payloads",
        source_symbol="mote.runtime.session.workspace.store.SessionWorkspace",
        disposition=RestoreSourceDisposition.NOT_PRODUCTION_DATA,
        logical_store="session-rollout",
        admission_contract="no production import or mount path",
        metadata_authority="session workspace ownership catalog",
        lifecycle_policy="session TTL releases artifact ownership before deletion",
    ),
)
ARCHIVE_CAPABILITY_DECLARATIONS = (
    ArchiveCapabilityDeclaration(
        capability_id="cold-durable-archive",
        online_archive_reader="",
        archival_generation=None,
        authority="product composition",
        disposition="not provided: binary document readers and verified backups are not cold archives",
    ),
)

__all__ = [
    "ACTIVE_STORE_DECLARATIONS",
    "ARCHIVE_CAPABILITY_DECLARATIONS",
    "MIGRATION_DEBT_DECLARATIONS",
    "RESTORE_COPY_DECLARATIONS",
    "RESTORE_SOURCE_CLASSIFICATIONS",
]
