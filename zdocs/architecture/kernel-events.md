# Kernel event planes

| Plane | Reliability | Backpressure | Converts to | May control execution |
| --- | --- | --- | --- | --- |
| RunEvent | bounded live observation | coalesce/drop non-terminal observations; terminal event retained | ObservationEvent | no |
| ObservationEvent | best-effort telemetry | backend-specific drop/sample | external telemetry | no |
| SessionEvent | durable replay truth | producer waits or fails transaction | projections and replay | only through explicit recovery input |

All planes correlate `run_id`, `attempt_id`, `operation_id` and, where applicable, `model_call_id`. Telemetry loss cannot change execution or replay. A `ProtocolIssue` first receives one explicit command semantic decision; only `ObservationDiagnostic` may be emitted directly as best-effort telemetry.
