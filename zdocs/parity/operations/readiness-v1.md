# Inference readiness v1

Readiness is a component verdict, not process liveness. The endpoint returns an
overall verdict plus the component records below. Each record carries the owner
plane, required flag, status (`ready|degraded|failed`), policy, admission result,
affected provider/operation scopes, stable error code and a redacted message.
Unknown required components fail closed. Raw principals, credentials and provider
payloads are forbidden in readiness output.

| component | owner plane | required | failure policy | accepts new requests | affected scope | stable error code |
| --- | --- | --- | --- | --- | --- | --- |
| generation | daemon | yes | fail closed | no | all | INFERENCE_GENERATION_NOT_READY |
| scheduler | daemon | yes | fail closed | no | all | INFERENCE_SCHEDULER_NOT_READY |
| receipt_outbox | daemon | yes | fail closed on hard failure; degraded during bounded replay | degraded only | all | INFERENCE_RECEIPT_AUTHORITY_NOT_READY |
| usage_ledger | daemon | yes | fail closed for budgeted requests | unbudgeted only | budgeted operations | INFERENCE_LEDGER_NOT_READY |
| credential_store | caller | yes | fail closed for affected immutable bindings | unaffected only | provider/credential revision | INFERENCE_CREDENTIAL_NOT_READY |
| artifact_store | daemon | conditional | fail closed for artifact-producing/consuming operations | unaffected only | artifact operations | INFERENCE_ARTIFACT_NOT_READY |
| connection_pool | daemon | yes | fail closed for affected protocol endpoint | unaffected only | provider/endpoint | INFERENCE_CONNECTION_POOL_NOT_READY |
| audit_policy | caller | yes | configured mandatory audit fails closed | policy dependent | governed operations | INFERENCE_AUDIT_NOT_READY |
| migration | daemon | yes | fail closed | no | all | INFERENCE_MIGRATION_NOT_READY |
| disk_capacity | daemon | yes | hard watermark closes new wire admission | reconciliation only | storage authority | INFERENCE_DISK_CAPACITY_EXHAUSTED |
| sqlite_integrity | daemon | current_shared | fail closed and preserve original bytes | no | shared daemon | INFERENCE_SQLITE_INTEGRITY_FAILED |

Overall `failed` means at least one required component failed for the requested
scope. Overall `degraded` means every required component can safely serve that
scope but at least one component is degraded. Only `ready` opens unrestricted
admission. A provider-scoped failure cannot fail open for that provider, but does
not reject a request proven unable to reach it. Hard budget, receipt corruption,
mandatory audit, migration and disk hard-watermark failures are global for their
authority.

Shared startup additionally requires SQLite integrity, WAL reconciliation, a
verified active generation and generation-suffixed discovery. During drain, new
admission is false while pinned executions remain resumable. `IN_DOUBT` backlog
is exposed and alerted; it is never represented as terminal success.
