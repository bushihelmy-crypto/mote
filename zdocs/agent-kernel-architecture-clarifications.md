# Agent Kernel Architecture Clarifications

> Status: implemented (initial enforcement baseline)
>
> Horizon: long-term framework architecture (10 years)
>
> Scope: clarify four architectural concerns raised after the typed run-output
> implementation: Role composition, event governance, public API complexity,
> and machine-enforced dependency boundaries.

## Implementation status

- 2026-07-23: Moved Graph terminal validation, commit, and resume out of
  `Role` into a Role-assembled `GraphOutputService`. `RunGraph` still receives
  narrow capabilities, so `executor` does not import `roles`; Agent and Graph
  execution remain distinct domain models while sharing the output transaction
  and fencing infrastructure.
- 2026-07-23: Added the session-owned `ROLLOUT_EVENT_TYPES` policy. It governs
  only persistence projection and does not change EventBus control/observation
  dispatch or restrict CLI, logs, tracing, and other observers from consuming
  the same events.
- 2026-07-23: Added the stable `mote.output` facade and `OutputContract.from_type`,
  `from_json_schema`, and `text` constructors. Lease, fence, journal, migration
  engine, and `OutputEngine` internals stay out of the ordinary output API.
- 2026-07-23: The existing zero-baseline layer-direction test remains the hard
  dependency guard. Added shrinking baselines for production function-local
  imports and runtime import cycles, plus a public output-facade contract test.
- 2026-07-23: Removed the Textual host/consumer cycle by extracting the shared
  message type, and removed the Device selector/Android-driver cycle by
  extracting the device backend contract. Slash-command registration primitives
  now live in a side-effect-free core module, while the registry owns one lazy
  built-in discovery import. The runtime import-cycle baseline is therefore
  zero; the discovery import remains explicit in the shrinking local-import
  baseline as a deliberate plugin boundary.
- 2026-07-23: Added replay compatibility proofs for legacy output payloads,
  unknown future event types, duplicate output facts, and output state across
  message compaction. The two-plane bus remains unchanged.
- 2026-07-23: Isolated each Graph compute expression in its own bounded worker
  executor. This removed cross-run default-executor contamination and restored
  the full RunGraph tool suite without merging Graph and Agent execution.
- 2026-07-23: Promoted 72 ordinary CLI port, human-channel, terminal/Textual,
  rich-renderer, Graph scheduler, sandbox-adapter, tool-config, background-task
  result, RunGraph, Markdown-agent, and Role component dependency imports to
  module scope. The local-import migration queue now retains only genuine
  optional, platform, plugin-discovery, and deliberate lazy-loading boundaries.
- 2026-07-23: Extracted dependency-free Git worktree probes into `common.vcs`,
  removing the inverted `common.const.paths` dependency on the richer git-state
  collector. Consumer registration primitives now live in a side-effect-free
  core, and the Textual UI class is separated from its CLI bootstrap composition
  root; host selection retains one intentional lazy import.
- 2026-07-23: Isolated POSIX raw-mode handling behind a platform adapter, so
  the host-neutral terminal port no longer imports `termios` or `tty` inside
  methods. Every retained local import is now machine-classified as an optional
  dependency, platform boundary, plugin discovery point, or lazy bootstrap;
  unknown internal boundaries fail the architecture suite.
- 2026-07-23: Split PDF, DOCX, and XLSX extraction into dependency-specific
  adapters selected by the document service; optional SDKs no longer appear in
  document-domain methods. Rich terminal construction is likewise isolated in
  its adapter, and sandbox consumers now import guard, adapter, and resource
  modules explicitly instead of relying on a dynamic package facade.
- 2026-07-23: Centralized optional durable-backend discovery in
  `loop.durable.plugins`. The loop factory now selects a typed backend factory
  without directly importing Temporal; missing SDK and fallback semantics remain
  owned by the durable subsystem boundary.
- 2026-07-23: Isolated half-block image rendering behind a Pillow/Rich adapter
  and Kitty PNG transcoding behind a Pillow encoding adapter. Core render and
  terminal-protocol modules retain deterministic text/raw-PNG degradation
  without importing optional imaging implementations inside methods.
- 2026-07-23: Made truecolor renderer tests explicitly opt into color instead
  of inheriting `NO_COLOR` from the host process. The assertions still verify
  exact brand and math styling, but are now deterministic in CI and agent shells.

## 1. Role remains the composition root

`Role` having high connectivity is not itself a design flaw. A composition root
must know how the runtime is assembled and which object owns each component's
lifetime. Moving that responsibility into global containers, implicit
singletons, or `Environment` would make isolation, recovery, and testing worse.

`Role` should continue to assemble and own the Agent runtime:

```text
Role
├── ContextManager
├── ThinkEngine
├── ReActLoop / AgentRunEngine
├── ToolExecutor
├── CommandChannel
├── Router
├── SessionManager
├── output services
├── lease coordinator
└── EventBus
```

The boundary is not "Role must assemble fewer things". The boundary is:

- `Role` owns assembly, the public facade, and component lifecycle.
- Agent algorithms stay in the Agent loop and its collaborators.
- Graph algorithms stay in `executor/tasks/bggraph/`.
- Shared mechanisms are injected through narrow interfaces.
- Concrete subsystem algorithms must not accumulate in `Role` merely because
  it can reach every component.

### Agent runs and Graph runs are deliberately different

An Agent run and a Graph run are not two implementations of one domain object:

| Agent run | Graph run |
| --- | --- |
| Open-ended and model-directed | Constrained by nodes and edges |
| Advances through observe/think/act | Advances through frontier scheduling |
| Completion is a semantic model decision | Completion follows graph terminal semantics |
| Uses turn and correction budgets | Uses node retry and recursion limits |
| Dynamic next action | Dependency- and router-driven next node |

They must not inherit a common `BaseRunEngine`, nor be hidden behind a service
that repeatedly branches on `RunKind`. That would erase domain semantics and
eventually create a new giant coordinator.

They may share execution-model-independent infrastructure:

```text
AgentRunEngine             GraphExecutionService
       │                            │
       ├──── OutputCommitter ───────┤
       ├──── RunLeaseCoordinator ───┤
       ├──── RunJournal ────────────┤
       └──── OutputPublisher ───────┘
```

Sharing an output transaction, lease backend, journal, or publisher does not
make the two runs the same thing. `RunKind` is useful as a persistence,
namespace, and observability tag; it must not become the discriminator of a
false unified runtime state machine.

### Graph-specific output behavior

Graph terminal validation and recovery should be owned by a Graph output
service in the Graph subsystem, while `Role` assembles that service and exposes
it to `RunGraph` through the existing capability boundary.

Conceptually:

```python
class GraphOutputService:
    async def finalize(self, terminal_value, contract, execution): ...
    async def resume(self, contract, execution): ...
```

This is a placement improvement, not an attempt to remove Graph behavior from
the Role-owned runtime. `executor` must still not import `roles`; the concrete
service is injected as a narrow capability.

## 3. Keep the two-plane event model

Mote's fundamental event distinction remains:

```text
EventBus
├── control plane: handlers may return ControlOutcome and affect execution
└── observation plane: subscribers may observe; return values are discarded
```

This distinction describes interaction semantics, not who is allowed to consume
an event. CLI, rollout recording, logs, tracing, metrics, and other observability
tools may all consume any event relevant to them.

Do not split the bus into mutually exclusive Domain, Presentation, and
Telemetry event families. That classification confuses the nature of an event
with a consumer's use of it. For example, `OutputCommittedEvent` may be consumed
simultaneously by:

- `RecorderSubscriber` for durable history;
- the CLI projector for user presentation;
- AG-UI and ACP adapters;
- metrics and tracing subscribers;
- diagnostic logs.

### Orthogonal event policies

Properties such as persistence and sensitivity are policies orthogonal to the
control/observation plane:

```text
plane:       CONTROL | OBSERVATION
policies:    durable, replayable, user-visible, telemetry-relevant, sensitive
```

An event can carry several policies without restricting its subscribers. The
recommended ownership is:

- EventBus owns control versus observation dispatch rules.
- `session` owns which observation facts are durable.
- CLI owns projection from Agent events to `ViewEvent`.
- observability integrations own metrics and tracing projection.
- a shared redaction boundary governs sensitive payloads before external logs
  or telemetry receive them.

Durability should preferably be an explicit session registry rather than a
property that forces `common/events` to know session storage policy.

### Observing control events

Observers may observe a control request, but only control handlers may influence
the host. Observer failures and return values must not affect the decision.

When the resolved decision is itself valuable for UI, audit, or replay, emit a
separate observation fact after control folding:

```text
PreToolUseEvent       -- control request
ToolUseResolvedEvent  -- observation of the resulting decision
```

This is not required for every control event. Add the resolved observation only
where the final outcome has independent value.

### Event compatibility rules

Durable events need stable type tags and explicit compatibility handling. Their
reducers must be pure: replay must not call providers, write external files, or
republish UI output. Compatibility tests should cover:

- old fixtures remain readable;
- unknown types are ignored;
- replay and compaction produce equivalent state;
- duplicate durable facts are idempotent or explicitly deduplicated;
- presentation projectors can evolve without migrating rollout history.

## 4. Keep the internal model rich and the public API small

Candidate, evaluation, correction, commit, publication, lease, fencing,
journal, and migration are valid internal concepts. The problem begins only if
ordinary users must understand all of them to run a typed Agent.

Expose three deliberate API levels.

### Level 1: ordinary Agent users

The common path should require only an output type and a run call:

```python
agent = Agent(output_type=Invoice)
result = await agent.run(message)
invoice = result.output
```

Decoder selection, representation negotiation, candidate lifecycle, commit,
publication, fencing, and migration remain internal defaults.

### Level 2: advanced Agent developers

Expose the concepts needed to customize typed output behavior:

```text
OutputContract[T]
OutputValidator[T]
OutputRetryPolicy
ValidationIssue
RunResult[T]
```

Provide convenience constructors instead of requiring manual assembly:

```python
OutputContract.from_type(Invoice)
OutputContract.from_json_schema(schema)
OutputContract.text()
```

### Level 3: runtime and backend developers

Infrastructure types belong in explicit advanced namespaces:

```text
RunLeaseCoordinator
CommitFence
RunJournal
OutputMigrationRegistry
OutputPublisher
```

They must not dominate top-level exports or ordinary IDE completion.

### Public facade policy

Top-level `__init__.py` files should export only deliberately supported entry
points. Internal modules may evolve, while the facade carries the compatibility
promise. `RunResult` should keep common fields direct and group diagnostic
detail rather than flattening every internal fact into its public surface.

Before adding a public concept, ask:

1. Must an ordinary caller actively operate it?
2. Can it be a policy or field of an existing public object?
3. Can it remain an internal event or infrastructure detail?

If the first answer is no and either later answer is yes, keep it out of the
ordinary public API.

## 5. Enforce architecture with machines

Documentation expresses intent; tests and pre-commit must prevent new
violations. Introduce enforcement incrementally so existing debt can only
shrink.

### Layer dependency guard

Parse production imports with the AST and enforce the repository's dependency
direction:

```text
common <- context/executor/router/session <- parser/think/loop
       <- roles <- environment <- cli
```

Stricter invariants include:

- `common` never imports an upper layer;
- `session` never imports `roles`, `environment`, or `cli`;
- `executor` never imports `roles`, `environment`, or `cli`;
- upper-layer behavior required by a lower layer enters through a Protocol or
  capability defined at the permitted boundary.

### Local-import guard

Production function-local imports are allowed only for a real boundary:

```text
optional dependency
plugin discovery
platform-specific dependency
documented cycle boundary
```

All ordinary imports must be module-level. A local import should carry a
machine-readable reason, and new unannotated local imports should fail an
architecture test. Tests are excluded from this rule.

A `cycle boundary` marker is architecture debt, not a permanent convenience.
Track known cases in a baseline that may shrink but may not grow unnoticed.

### Import-cycle guard

Build the production module dependency graph and find strongly connected
components. Record existing cycles temporarily, reject new cycles, and delete
baseline entries as cycles are removed. The end state is a zero-cycle baseline,
not a permanently growing exception list.

### Interface and public API guards

Add checks for:

- leaf-interface dependency rules;
- Protocol runtime requirements where runtime checking is intended;
- accidental concrete implementation imports across layers;
- stable public facade exports and callable signatures;
- durable event fixture compatibility.

Do not enforce an idealized rule that contradicts current architecture. For
example, if an interface legitimately refers to schema types under
`TYPE_CHECKING`, the guard should model that distinction instead of banning all
schema references mechanically.

### Rollout order

1. Add read-only reports and record the current baselines.
2. Make pre-commit reject only newly introduced violations.
3. Remove existing violations subsystem by subsystem.
4. Delete empty baselines and make the clean invariant permanent.

## Non-goals

This guidance explicitly rejects:

- removing Role's composition-root responsibility;
- treating Agent and Graph execution as one domain state machine;
- splitting events by consumer and preventing cross-cutting subscribers;
- exposing all runtime concepts through the ordinary Agent API;
- using architecture baselines as permanent permission for new debt.
