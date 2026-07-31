# Gateway backup and restore v1

Status: Gate 0 frozen contract. Scope: Embedded and same-host Shared Process.

The backup barrier increments `backup_epoch` and `admission_epoch`, closes new
wire authorization, waits for the declared callers, flushes receipt/outbox and
artifact publication authorities, then uses SQLite online backup. Permits from
an earlier epoch are rejected on first consumption. A permit already committed
to `SEND_COMMITTED` is preserved for reconciliation and is never replayed.

`APPLICATION_CONSISTENT` requires every declared caller acknowledgement and all
component, generation, key and artifact digests. A missing caller limits the cut
to `DAEMON_CONSISTENT`; an unverified component limits it to
`CRASH_CONSISTENT`. Consistency is never promoted by operator assertion.

Restore order is signature, manifest/schema, component digests, key versions,
daemon authority, caller journals, open-receipt reconciliation, then readiness.
Restore occurs into an empty isolated directory. Missing/corrupt blobs, unknown
keys or digest mismatch keep readiness false and produce an explicit impact
report. The original corrupt authority is preserved byte-for-byte.
The local apply service additionally requires an explicit approval bound to the
verified backup digest and an exclusively stopped daemon. It removes the new
isolated authority if immutable audit commit fails, so a restore never becomes
visible without its operation record. In-place replacement remains forbidden.

Frozen operational objectives are defined in `inference-slo-v1.md`. Backups and
reports use the existing Artifact publication and retention owners; no private
backup catalog or cleanup worker is permitted.
