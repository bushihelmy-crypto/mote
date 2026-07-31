# Recover Shared daemon

Set readiness false and stop new admissions. Verify advisory-lock ownership,
process incarnation and generation-suffixed socket before moving stale files.
Recover SQLite WAL, validate generation/artifact digests, drain outbox and
reconcile committed non-terminal receipts. Restore READY only after all mandatory
component checks pass. Never delete a corrupt database or replay permits blindly.
