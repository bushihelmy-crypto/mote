# mote package architecture

This document is the migration contract for the new package layout.  It
describes ownership, not merely directory names.

## Dependency direction

```text
contracts <- kernel <- runtime <- orchestration <- product
```

Concrete model providers and external integrations implement lower-layer
protocols and are assembled by the product.  A lower layer must never import a
higher layer.

## Layer ownership

### `contracts`

Stable, implementation-free messages, commands, events, effects, identifiers,
tool-call and output DTOs, plus versioned persisted formats.  It performs no
I/O and owns no resource lifecycle.

The first extracted contracts are normalized model actions
(`TextAction`, `ToolCallAction`, `FinalCandidateAction`, `ModelTurn`), the
persisted `ToolEffect` enum, canonical tool-argument serialization, and the
complete typed-output wire model (`OutputContractId`, binding decisions,
validation decisions, lifecycle states, committed outputs, and `RunResult`).
These output types have one owner in `contracts/output.py`; Common no longer
re-exports or shadows them.

### `kernel`

The semantics of one agent run.  Its center is `FlowEngine`, the successor to
the old loop.  Flow decides when the agent observes, thinks, interprets, acts,
validates output, waits, and finishes.  `think` performs one model inference;
`parser` converts XML/native model protocols into kernel commands.

`AgentSpec` owns the model-facing identity, prompts, command protocol, cognition
settings, and declared tools of one Agent. `AgentRunState` owns transient Flow
signals. Runtime's flat `RoleSchema` extends `AgentSpec` with deployment and
reliability policy, so existing persisted configuration does not require a
nested-format migration.

`kernel/output` owns typed output contracts, schema decoding, retry policy, and
the model-facing structured-output context source. Runtime owns output migration
registries, lifecycle restoration, validation event recording, commit fencing,
durable commit, and publication; those reliability mechanisms are deliberately
absent from the public output facade.

`RunGraph` is not the kernel graph.  It remains a model-facing product tool
whose implementation runs an orchestration task graph.

### `runtime`

Safe and reliable execution of kernel commands: tool execution, permissions,
effect reservation and commit, journals, checkpoints, leases, recovery,
sandboxing, secrets, and exactly-once output publication.

The session subsystem now lives in `runtime/session`; journal, replay,
checkpoint, snapshot, and persisted terminal/browser/kernel state have one
runtime owner.

Optional distributed durability adapters live in `runtime/durable`. Temporal is
an adapter over Kernel's `DurableBackend` contract; the Kernel discovers it by
entry point and never imports the third-party SDK eagerly.

Model routing is being decomposed by ownership rather than retained as a sixth
layer. Provider-neutral responses live in `contracts/models`; routing request,
decision, and model-card semantics live in `kernel/models`; the reliable model
gateway, cost accounting, rate-limit state, and OAuth credential lifecycle live
in `runtime/models`. Deterministic complexity signals and routing rules are
Kernel semantics rather than provider implementation details. Runtime routing
strategies that call real model clients, plus operator hold/expiry state, live in
`runtime/models/routing`.

Provider-neutral client machinery lives in `runtime/models/clients`: base client
lifecycle, Context, registry, retry, health, recovery, credential rotation,
response validation, and provider wire transforms. Concrete SDK adapters remain
separate Product integrations so importing Runtime does not select a vendor.
Concrete provider SDKs and Squilla product policy are the remaining migration
work before the transitional top-level `router` package is deleted.

The former top-level `context` and `executor` packages now live at
`runtime/context` and `runtime/tools`.  Kernel depends only on the Toolset and
tool-spec contracts in `kernel/tools`; it never imports Runtime execution.

The sandbox now lives at `runtime/sandbox`, beside permissions and tool
execution.  Product tools request sandbox capabilities through Runtime rather
than owning platform isolation themselves.

The former `roles` package now lives at `runtime/agent`.  New serialized roles
use the stable type ID `mote.agent.role.v1`; the previous Python path is handled
only by the explicit legacy reader and is never written again.

### `orchestration`

Relationships between runs: child agents, background tasks, task graphs,
scheduling, concurrency quotas, messaging, and worker residency.

Background tasks and the model-facing `RunGraph` task-graph implementation now
live under `orchestration/tasks`.  Runtime recognizes their return value through
a structural marker and does not import the orchestration implementation.

The Agent component graph receives `build_background_task_pool` from the Product
composition root. Kernel and Runtime see only the Contracts-level
`BackgroundTaskService` port; pool construction, output-store wiring, terminal
notifications, and cancellation policy remain owned by Orchestration.

### `product`

The coding-agent distribution: built-in Toolsets, Skills, LSP, code indexing,
prompts, configuration, CLI, and UI.  Product tools implement lower-layer tool
protocols; they do not become kernel concepts.

`CodingAgentFactory` is the standard Product composition root. It supplies the
built-in Toolsets and Orchestration background-task builder to Runtime Agents;
CLI and model-spawned child Agents share this construction path.

Product-owned protocol publication assets live under
`product/integrations/<protocol>`; ACP registry metadata is not a framework
package. Private binaries used by one built-in Toolset are colocated under that
Toolset's `assets` directory; there is no generic top-level `vendor` owner.

## Core execution path

```text
Product/CLI
    -> Runtime RunCoordinator
        -> Kernel FlowEngine
            -> ThinkEngine
            -> Parser / CommandChannel
            -> ToolDispatcher protocol
                -> Runtime ToolExecutor
                    -> Product Toolset / Tool
```

For the `RunGraph` tool only:

```text
FlowEngine -> ToolExecutor -> Product RunGraph tool
                              -> Orchestration TaskGraphRunner
```

## Migration rule

### Deleted `common` invariant

`common` is not a sixth architectural layer. It has been physically deleted,
and architecture tests forbid recreating the directory. Ownership is resolved
by behavior, not by how widely a module is reused:

```text
common schema / IDs / events / ports / pure cross-boundary values -> contracts
common Agent execution abstractions and prompt semantics          -> kernel
common config loading, IO, lifecycle and process services         -> runtime
common multi-run scheduling and coordination                      -> orchestration
common human text, localization and distribution defaults         -> product
```

The first extraction moved localization to `product/i18n`; hooks, file
watching, observability and circuit-breaker services to `runtime`; the shared
periodic loop to `runtime/scheduling`; and pure breaker/hostname values to
`contracts`. The second extraction moved all structural interfaces into
`contracts/ports`, plus stable error codes, root errors, tool errors and output
errors into `contracts/errors`. The legacy `common.exception` package is now
deleted: serializable error envelopes and cross-layer domain errors live in
`contracts/errors`, provider classification and recovery execution live in
`runtime/errors`, and media-generation failures live in `product/errors`.
Permission decisions and facts now live in `contracts/permissions`; immutable
event records, control outcomes, and rewrite provenance live in
`contracts/events`. The legacy `common/events` package is deleted: EventBus
dispatch, ambient binding, scopes, streaming, subscribers and Runtime adapters
live in `runtime/events`. Kernel Flow emits observations only through
`kernel.telemetry`; Runtime binds that narrow capability inside `set_bus`, so
Kernel never imports the Runtime event transport. Structured-output stream
accumulation is Kernel execution state in `kernel/output_stream`, while external
resource reporting lives in `runtime/reporting` and is injected into ThinkEngine.
The legacy `common/schema` package is deleted. Conversation messages, queues,
documents, environment DTOs, interaction DTOs, run leases, completion
decisions, Think results, and serialization primitives live in `contracts`;
stable message/context constants and persisted-output markers live in
`contracts/constants`; background-task status lives with
`orchestration/tasks`. Validated
cross-layer settings for devices, hooks, LSP, permissions, sandboxing, file
watching, and web search live in `contracts/settings`; Runtime and Product own
their activation and enforcement rather than the DTO definitions.
The legacy `common/prompt` package is also deleted. Prompt symbols, system
prompt composition, model memory sections, output protocol instructions, and
compaction prompts are Kernel execution semantics under `kernel/prompt`.
Command-channel behavior now lives with `kernel/parser`; Flow-owned duplicate
turn guards and XML recovery no longer reside in generic Common utilities.
The legacy `common/base` package is deleted: Agent identity is a Contract,
ThinkEngine abstraction belongs to Kernel, and persistent Role registration
belongs to Runtime. Task constants live with Orchestration; built-in tool
limits live with Product Toolsets; shared tool wire constants live in Contracts.
Resource projection, Workspace ownership, cleanup, append-only Ledgers, run
Journals, durable backends, secret storage and redaction are Runtime services.
Kernel retains only effect-aware Flow recovery semantics and Think checkpoint
state; it receives the Runtime journal capability through composition and never
imports Runtime persistence implementations.
Configuration DTOs live in `contracts/config` and have no filesystem methods;
YAML parsing, source layering, environment overrides, diagnostics, watching and
activation live in `runtime/config`. Langfuse and breaker activation occurs at
the Runtime composition boundary rather than in Pydantic validators.
DiskWriter, Journal and filesystem primitives live in `runtime/disk`. Kernel
Flow receives only an async write-drain capability, so its transaction ordering
does not depend on a global disk implementation. Spawn request data lives in
`contracts/spawn`; ambient control binding and spawn execution live in Runtime;
the concrete multi-Agent control plane remains Orchestration-owned.
Deterministic cross-layer text transforms live in `contracts/text`. They may
depend only on stdlib and sibling Contracts, never higher-layer implementations.
Docstring introspection and model tokenization are dependency-free Contracts;
XML streaming belongs to Kernel Parser; process execution, media codecs,
sanitization, retry policy, logging, and Agent recovery decorators belong to
Runtime. No compatibility import path or forwarding package remains.

Files are moved, not copied.  A migrated implementation has one owner and one
runtime path.  Compatibility re-exports are not retained for internal APIs.
Persisted Python module paths must be replaced by stable versioned type IDs
before moving the corresponding persisted classes.
