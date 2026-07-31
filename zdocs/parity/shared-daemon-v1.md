# Shared daemon v1 contract

Status: Gate 0 frozen contract. Scope: same-host Shared Process only.

## Ownership and state

The Product supervisor is the sole launcher. The daemon state machine is
`ABSENT → STARTING → READY → DRAINING → STOPPED`, with any live state allowed to
enter `CRASHED → RECONCILING → READY` only after durable recovery completes.

The per-user runtime directory is mode `0700`; database, discovery, lock and key
files are `0600`; the UDS is owner-only. An advisory-lock holder is the sole
daemon and SQLite writer. PID metadata is diagnostic evidence, never ownership.
A stale socket may be moved aside only while holding the lock and after both
process-incarnation and connect/probe checks fail.

Every daemon uses a generation-suffixed socket. A stable discovery record is
atomically switched only after the new generation is READY. Upgrade drains the
old socket; rollback points discovery to a still-compatible generation without
overwriting either socket.

## Trust and reconnect

Accept verifies UDS peer UID and process identity. Handshake binds Application
identity, caller incarnation, daemon socket generation, tenant scope and expiry.
Caller-controlled metadata cannot assert identity or tenant. Session credentials
are short-lived and revocable; they sign permits but cannot mint daemon identity.

Reconnect negotiates N/N-1, then queries by execution ID, receipt revision and
last event cursor. It never replays start, authorize or application-message calls.
Events are at-least-once and sequence-deduplicated. EOF is not a terminal fact.

## Persistence

Shared SQLite is local-filesystem-only and single-writer. WAL, foreign keys,
bounded busy timeout and `synchronous=FULL` are mandatory. Network waits are
forbidden inside transactions. Permit consumption, `SEND_COMMITTED`, settlement
and outbox append retain full durability. Hard disk watermark rejects new wire
authorization while permitting bounded reconciliation and cleanup.

Online encrypted backups bind schema, generation and digest. Startup runs
`quick_check`; restore drills are periodic. Corruption preserves the original,
sets readiness false, restores only from verified backup/evidence, and reconciles
every open receipt. The daemon never deletes or recreates a corrupt authority.

## Lifecycle

Restart recovers WAL, activation state, receipts, outbox and artifact references
before READY. Committed non-terminal executions reconcile or become `IN_DOUBT`.
Shutdown publishes DRAINING, rejects new executions and drains pinned streams to
their original deadline. Last-client disconnect does not implicitly stop daemon.

Cluster placement, quorum, cross-host mTLS, distributed fencing and multi-active
daemon behavior are explicitly outside this contract.
