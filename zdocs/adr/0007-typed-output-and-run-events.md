# ADR 0007: Typed output and stable run events

Status: Accepted

## Decision

`OutputT` remains invariant from `OutputContract[OutputT]` through the runtime output engine, the narrow `OutputTransactionPort[OutputT]`, Kernel output operations, execution state/result/graph/engine, and `Role[DepsT, OutputT]`. History and inference mutations remain on the non-generic `ExecutionTransactionPort`; only accepted-output staging and terminal commit use the generic port. Kernel consumes a narrow output port that exposes evaluation, restore state, encoding, and terminal state without importing Runtime.

`ExecutionState.turn` is the closed union `NoModelTurn | ModelTurn | CandidateSelection`. `CandidateSelection` validates its non-negative index and that it addresses an existing candidate. Nodes own phase interpretation; `None` and tuple protocols are forbidden.

Run observation is deliberately non-generic. `RunSucceeded` carries only an immutable `RunCompletionSummary` containing committed status, candidate ID, output contract ID, and presentation kind. Actual output values travel only in `ExecutionEngine[OutputT].run()` results. Wire and checkpoint schemas continue to use explicit IDs and versions, never runtime generic metadata.

## Rejected alternatives

- Generic run events leak result transport into telemetry and force heterogeneous buses to erase dishonestly.
- Genericizing the whole transaction port couples unrelated history/tool persistence to output type.
- Casting restored or committed values merely hides a broken chain.
- A free `TurnT`, `None`, or tuple state does not describe the execution invariant.

## Compatibility and migration tests

Runtime behavior, output wire schema, recovery ordering, and persistence stay unchanged. Pyright contract cases prove matching contract/engine/transaction/graph composition and reject crossed output types. Runtime tests cover candidate selection validation, accepted staging, terminal commit, restore, and stable success summaries.
