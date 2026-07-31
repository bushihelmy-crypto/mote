# ADR: LSP reliable derivation from committed file transitions

Status: accepted
Date: 2026-07-31

## Decision

LSP document synchronization is a recoverable projection of committed File Operations facts. Its authoritative input is `FileTransactionCommittedEvent` after the session journal commit, never `FileMutatedEvent` telemetry. External changes remain owned by the file-version watcher and are not reclassified by LSP. Diagnostics remain advisory output and may use telemetry because losing a diagnostics notification does not lose file authority.

The session `EventFabric` owns delivery and the durable checkpoint. The LSP projection uses `Reliability.RELIABLE`, per-stream ordering, bounded backpressure, retry, and quarantine. Work identity is the committed envelope identity and the exact `FileVersion` tuple; replay is idempotent because synchronizing the same confirmed path/version again has no authoritative side effect. A poison item may be quarantined so unrelated later file transitions can converge.

The projection exposes degradation through the existing EventFabric/subscription health snapshot. Language-server startup or save failure is raised to the worker so retry and quarantine, rather than silent best-effort swallowing, determine progress. Query APIs and diagnostics continue to degrade to empty advisory results.

## Lifecycle and recovery

The Role session is the single lifecycle owner. It constructs the optional LSP service before EventFabric startup, registers exactly one reliable subscription, and closes the service with the session. On restart, the dispatcher reloads the persisted checkpoint and replays committed envelopes. No independent LSP queue, process singleton, telemetry subscriber, or second cursor exists.

## Consequences

- A successful tool body is insufficient to trigger LSP; only a durable committed transition does.
- The old `FileMutatedEvent -> LspService` inference edge is deleted. `FileMutatedEvent` remains an observational contract for other telemetry consumers only.
- Diagnostics delivery is intentionally advisory and must never become a correctness dependency.
- File writes outside managed File Operations require the watcher/attribution path; LSP does not infer authority from filesystem timing.
