# Execution recovery matrix

`SessionEvent` is the only replay truth. `TransactionRecord` exists only while Runtime reconciles an operation; it cannot be read as Agent history.

| Crash frontier | Durable identity | Recovery decision | Owner |
| --- | --- | --- | --- |
| before target resolve | run/attempt | resolve anew | Runtime inference |
| target resolved, request not finalized | target lease + capability fingerprint | validate lease; rematerialize tools and rebuild request if invalid | Runtime inference |
| request finalized, call not issued | model_call_id + request fingerprint | issue once under a new attempt fence | Runtime inference |
| call issued, response unknown | model_call_id + provider resume/idempotency identity | query/resume when supported; otherwise typed conflict, never blind replay | Runtime inference reconciler |
| response completed, turn not recorded | model_call_id + attempt fence | record the same `InferenceResult` idempotently | execution transaction |
| tool call recorded, effect unknown | effect operation_id | EffectLedger reconciliation; transaction port does not claim remote exactly-once | Runtime tools |
| tool result produced, not recorded | tool operation_id + snapshot revision | record the same result; stale registry revision conflicts | execution transaction |
| output rejected | rejection operation_id | return already-applied or append once | execution transaction |
| candidate accepted, not staged | candidate + validator/migration identity | evaluate only from the persisted candidate using recorded identities | Kernel output |
| `AcceptedOutput` staged | staged_output_id | reuse the same immutable DTO; evaluation is forbidden | execution transaction |
| terminal commit started | terminal operation_id + run fence | unique reconciler completes or reports fenced/conflict | Runtime transaction |
| terminal committed, checkpoint remains | terminal event identity | remove checkpoint as an infrastructure cleanup | Runtime reconciler |
| publication pending | committed output identity | retry delivery; never alter committed output | Runtime output delivery |

Cancellation and commit use the same expected revision. Exactly one transition wins; recovery returns either `cancelled` or `terminal_committed`. A late worker is rejected by the run fence, while an inference worker is independently rejected by the attempt fence. Target lease, attempt fence, and run fence are never interchangeable.

Failover may reuse projection assets only when the projection compatibility key is identical and the provider contract explicitly permits continuation of the same `model_call_id`. A route change otherwise returns `TargetInvalidated` and restarts resolve → materialize → project → negotiate → assemble.
