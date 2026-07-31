# IN_DOUBT reconciliation v1

Status: Gate 0 frozen contract.

The daemon may collect immutable receipt, authoritative provider query, verified
webhook and artifact-digest evidence and publish a durable `ResolutionProposal`.
It cannot decide logical success/failure, settle caller usage or append a caller
terminal. The caller logical owner validates proposal identity, strategy,
generation and evidence, appends exactly one decision, and returns a durable
`OwnerAcknowledgement`.

The state machine is `OPEN → AUTO_RECONCILING → EVIDENCE_AVAILABLE →
OWNER_ACTION_REQUIRED → OWNER_APPLIED | OWNER_REJECTED`. Proposal and
acknowledgement replay are idempotent; a conflicting acknowledgement fails
closed. Evidence priority is provider terminal receipt/query, verified webhook,
durable provider acknowledgement, then local receipt/wire observation. Logs,
traces and timeout guesses never prove success.

Scans have bounded concurrency, attempts, provider-query budget and backoff.
Strategy deadline, evidence conflict, missing key/artifact or unavailable query
converges to `OWNER_ACTION_REQUIRED`. A permanently offline caller remains
explicitly unresolved under retention and alerting; it is not garbage-collected
or force-completed. Manual actions are restricted to
`reconciliation-actions-v1.yaml` and remain auditable.
