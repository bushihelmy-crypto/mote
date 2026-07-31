# Shared daemon runbook

## Diagnosis
Verify owner lock, discovery permissions, process incarnation, socket generation, SQLite quick-check, queue and event-loop signals, and every readiness component.

## Containment
Close new admission for the affected generation. Preserve discovery, socket, database, WAL and process evidence. Stop an automatic restart loop after its bounded budget.

## Recovery
Start a replacement generation, verify readiness and N/N-1 negotiation, atomically publish discovery, then drain the old daemon while preserving resumable executions.

## Verification
Confirm authenticated RPC, receipt/event cursor resume, zero unexpected wire requests, pinned-generation completion and stable readiness through the recovery window.

## Escalation
Escalate to daemon operations and inference storage when ownership, SQLite integrity or discovery authority cannot be proved.

## Forbidden actions
Never delete a live or stale-looking socket without the supervisor lock and failed process/socket probes. Never repoint discovery to an unverified generation.
