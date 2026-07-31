# Storage and backup runbook

## Diagnosis
Inspect redacted disk, SQLite integrity, receipt/outbox, ledger, artifact publication/GC, mandatory audit and restore-drill signals. Resolve the exact authority and consistency class.

## Containment
At the hard watermark or integrity failure close new wire admission while preserving bounded reconciliation. Preserve original database bytes and every durability root.

## Recovery
Restore into an empty directory from a verified backup, validate schema, digests, keys and artifacts, replay outboxes, reconcile open receipts, and reopen only the verified authority.

## Verification
Run integrity checks, confirm receipt/outbox and ledger invariants, artifact publication visibility, legal holds, audit durability and the declared recovery consistency cut.

## Escalation
Escalate to inference storage, artifact operations, security operations or recovery operations according to the failed authority.

## Forbidden actions
Never delete receipts, outboxes, activation/audit history, legal holds or GC roots. Never promote an unverified or degraded backup consistency class.
