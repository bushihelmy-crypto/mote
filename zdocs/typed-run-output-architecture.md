# Typed Run Output Architecture

> Status: design plan
>
> Horizon: long-term framework architecture (10 years)
>
> Scope: make a run's final result a typed, validated, durable, provider-independent
> first-class concept. This is not a plan for another tool-result wrapper or retry
> helper.

## 1. Decision

Mote will model completion as a committed run output, not as an incidental final
message, an `End` command, absence of tool calls, or a synthetic framework tool.

The stable conceptual pipeline is:

```text
provider wire protocol
        ↓
provider-independent ModelTurn + AgentAction
        ↓
OutputEngine: decode → validate → accept → commit
        ↓
RunResult[T]
        ↓
CLI / parent agent / RunGraph / AG-UI / ACP / webhook presenters
```

OpenAI tool calls, native structured responses, Anthropic tool use, XML, and
prompted JSON are only wire representations. They must not define loop semantics.

Every run has an output contract. Natural-language output is the ordinary
`OutputContract[str]`, not an untyped special case.

## 2. Why this is a run-level concern

Mote already has strong typed execution infrastructure:

- typed tool inputs and generated JSON Schema;
- `ToolResult` with structured data, media, file changes, errors and retention;
- `ErrorReport`, error codes, retryability and recovery actions;
- provider response validation and recovery;
- `ToolEffect`, the effect ledger and durable run journal;
- typed EventBus events and crash-safe replay;
- Pydantic-backed graph inputs, state and node parameters.

Those facilities do not yet define the contract of a completed run. Today a
native response with no tool calls or an XML `End` can terminate the loop and the
result remains a `Message`. A future-proof runtime instead needs to distinguish:

```text
model produced a candidate
candidate decoded successfully
candidate passed structural and semantic validation
accepted value was durably committed
committed value was published to consumers
```

These are separate facts with different retry and recovery semantics.

## 3. Goals

The architecture must support:

- `Agent[DepsT, OutputT]` and `RunResult[OutputT]` as the single run API;
- Pydantic models, dataclasses, `TypedDict`, unions, containers, primitives,
  plain JSON Schema, text and binary/artifact results;
- structural, semantic and policy validation;
- synchronous and asynchronous validators;
- provider-native structured output where reliable;
- semantic-output-as-tool wire encoding where necessary;
- XML and prompted-JSON fallback;
- bounded model correction without conflating it with transport/tool retries;
- exact crash recovery of candidate, validation, commit and publication state;
- output schema and validator version evolution;
- RunGraph typed terminal results and typed child-agent results;
- typed partial streaming without weakening final strict validation;
- deterministic testing and fault injection at every durable boundary;
- high availability across process crashes and distributed workers.

## 4. Non-goals

- Do not replace `ToolResult`; it remains the contract of tool execution.
- Do not add a second exception or general retry framework.
- Do not route final output through ordinary `ToolExecutor` execution.
- Do not place runtime Python types or validator callbacks in `RoleSchema`.
- Do not place per-run schemas in the cacheable static system-prompt prefix.
- Do not let validators perform external writes.
- Do not preserve permanent `run()`/`run_typed()` dual APIs.
- Do not design around the obsolete code-review tool. Structured orchestration
  and typed graph completion belong to RunGraph.

## 5. Architectural invariants

The following are hard constraints and should gain architecture tests:

1. `FinalCandidate` never enters the ordinary `ToolExecutor`.
2. The loop never interprets a provider wire shape as completion.
3. A run succeeds only after its output is durably committed.
4. Validation before commit cannot perform external writes.
5. A run has at most one committed output.
6. Publishing failure never causes another model call, validator run or tool call.
7. Schema/version mismatch fails closed unless an explicit migration exists.
8. Model correction feedback and operational errors are different contracts.
9. `OutputContract` is immutable; `OutputState` is folded from events.
10. Session, run, turn, model attempt, action and candidate have distinct IDs.
11. Provider-specific structured-output behavior stays in wire bindings.
12. Text is an output contract, not an escape hatch around output semantics.
13. Once output is committed, no further agent action may execute in that run.
14. Cancellation and budget gates always dominate automatic correction.

## 6. Identity hierarchy

The runtime must explicitly model:

```text
Session
  └── Run
       ├── OutputContract
       └── Turn
            └── ModelAttempt
                 └── AgentAction
                      └── FinalCandidate
```

An output contract belongs to a run, not permanently to a session or Role. A
single session may consequently produce different result types over time, for
example a plan in one run and an execution report in the next.

Every durable record and trace must carry the smallest relevant identity set:

- `session_id`
- `run_id`
- `turn_id`
- `attempt_id`
- `action_id`
- `candidate_id`

## 7. Provider-independent model semantics

Replace the assumption that `ThinkResult(content, tool_calls)` fully describes a
turn with a normalized semantic result:

```python
@dataclass(frozen=True)
class ModelTurn:
    content: str
    actions: tuple[AgentAction, ...]
    provider_metadata: ProviderMetadata
    usage: TokenUsage
```

The action union should include at least:

```python
AgentAction = (
    TextAction
    | ToolCallAction
    | FinalCandidateAction
    | DelegationAction
    | PauseAction
    | RefusalAction
)
```

`FinalCandidateAction` carries raw, untrusted candidate data and provenance:

```python
@dataclass(frozen=True)
class FinalCandidateAction:
    action_id: str
    candidate_id: str
    contract_id: str
    raw: RawValue
    representation: OutputRepresentation
    provider_call_id: str | None = None
```

The parser/channel layer converts provider representations into this union. A
provider may encode final output as a tool call, but the adapter must classify it
as `FinalCandidateAction` before dispatch. It is not registered as a `BaseTool`
and never participates in tool permissions, effects, discovery or tool metrics.

## 8. Action dispatch

The loop delegates actions by semantic kind:

```text
ToolCallAction       → ToolExecutor
FinalCandidateAction → OutputEngine
DelegationAction     → AgentRuntime
PauseAction          → SuspensionController
RefusalAction        → CompletionPolicy
```

The action dispatcher enforces turn-level legality. The default policy is:

| Actions in one model turn | Decision |
| --- | --- |
| Multiple ordinary tool calls | Existing execution policy |
| One final candidate | Validate it |
| Final candidate plus any ordinary tool | Reject the turn |
| Multiple final candidates | Reject unless the contract explicitly accepts a candidate set |
| Final candidate plus delegation | Reject the turn |
| Final candidate plus pause | Pause wins; do not accept the candidate |
| Any action after output commit | Protocol violation |

Final submission must be an unambiguous linearization point. A model cannot
declare completion while also scheduling more side effects.

## 9. Output contract

An output contract is immutable, reusable run description data:

```python
@dataclass(frozen=True)
class OutputContract(Generic[OutputT]):
    contract_id: OutputContractId
    schema: SchemaDocument
    decoder: OutputDecoder[OutputT]
    validators: tuple[OutputValidator[OutputT], ...]
    retry_policy: OutputRetryPolicy
    representations: tuple[OutputRepresentation, ...]
    version: SchemaVersion
```

It must not hold attempt counters, accepted values, mutable callback state or
session references. Those belong to `OutputState` and run-scoped services.

The default text contract uses the same machinery:

```python
Agent[DepsT, str](output_contract=TextOutputContract())
```

## 10. Schema and codec abstraction

Pydantic is the primary official adapter but not the core abstraction. The
framework owns a narrow codec protocol:

```python
class OutputDecoder(Protocol[OutputT]):
    @property
    def schema(self) -> SchemaDocument: ...

    def decode(self, raw: RawValue) -> OutputT: ...

    def encode(self, value: OutputT) -> JsonValue: ...
```

Initial implementations:

- `PydanticOutputDecoder`
- `TypeAdapterOutputDecoder`
- `DataclassOutputDecoder`
- `JsonSchemaOutputDecoder`
- `TextOutputDecoder`
- `ArtifactOutputDecoder`
- `UnionOutputDecoder`

`SchemaDocument` is canonical framework data:

```python
@dataclass(frozen=True)
class SchemaDocument:
    dialect: str
    canonical: JsonObject
    fingerprint: str
```

Canonicalization must be deterministic. Schema fingerprints are hashes of the
canonical document, not Python module/class names.

This boundary protects the runtime from Pydantic major-version changes and
allows future Protobuf, Avro or other typed formats without changing the loop.

## 11. Validation pipeline

Validation is an explicit pipeline:

```text
decode
  → structural validation
  → semantic validation
  → policy validation
  → accept
```

Validator metadata is part of the contract:

```python
class OutputValidator(Protocol[OutputT]):
    name: str
    version: str
    stage: ValidationStage
    determinism: Determinism
    effect: ValidatorEffect

    async def validate(
        self,
        value: OutputT,
        context: ValidationContext,
    ) -> ValidationDecision[OutputT]: ...
```

Normal validation outcomes are values, not exceptions:

```python
ValidationDecision = Accept[T] | Corrected[T] | Reject | RetryLater
```

- `Accept` passes the value unchanged.
- `Corrected` applies a deterministic canonicalization and records provenance.
- `Reject` means the candidate is invalid and produces model correction feedback.
- `RetryLater` means validation infrastructure is temporarily unavailable.
- An exception means the validator itself failed and enters operational recovery.

Validator effects are limited to:

```python
ValidatorEffect = PURE | READ_EXTERNAL
```

External writes are forbidden before commit. Business changes must be explicit
tool or RunGraph steps completed before final submission.

## 12. Correction feedback versus operational errors

`ErrorReport` remains the canonical operational error envelope. Invalid model
output needs a separate provider-neutral correction contract:

```python
@dataclass(frozen=True)
class CorrectionFeedback:
    code: str
    summary: str
    issues: tuple[ValidationIssue, ...]
    candidate_id: str | None
    directive: RetryDirective
```

```python
@dataclass(frozen=True)
class ValidationIssue:
    path: tuple[str | int, ...]
    code: str
    message: str
    expected: JsonValue | None = None
    received: JsonValue | None = None
```

The output engine produces structured feedback. The wire binding renders it as:

- a native output-tool result;
- an OpenAI/Anthropic correction message;
- an XML validation block;
- a prompted-JSON correction instruction.

The output engine must never build provider messages or XML itself.

## 13. Completion policy

Completion becomes a provider-independent strategy:

```python
class CompletionPolicy(Protocol):
    async def evaluate(
        self,
        turn: ModelTurn,
        run_state: RunState,
        output_state: OutputState,
    ) -> CompletionDecision: ...
```

```python
CompletionDecision = Continue | ValidateCandidate | Complete | Suspend | Fail
```

Rules:

- With a non-text contract, plain assistant text is not completion.
- With a text contract, a normal final text response becomes a text candidate.
- Absence of tool calls, provider finish reason and XML `End` are signals used by
  a wire binding; none is independently proof of successful completion.
- Only a committed output permits `Complete`.
- Budget exhaustion, cancellation and retry exhaustion produce explicit terminal
  states; they never fabricate an output value.

## 14. Output state machine

`OutputEngine` owns a deterministic state machine:

```text
IDLE
  → CANDIDATE_RECEIVED
  → DECODING
  → VALIDATING
      ├── REJECTED → AWAITING_CORRECTION
      ├── RETRY_PENDING
      ├── FAILED
      └── ACCEPTED
            → COMMITTING
            → COMMITTED
            → PUBLISHED
```

Required properties:

- candidate IDs are unique within a run;
- validation is repeatable whenever every validator is `PURE`;
- a rejected candidate cannot later become committed without a new candidate;
- a committed output is immutable;
- publication can retry without repeating validation or model work;
- replaying events produces the same state as uninterrupted execution.

## 15. Durable events and replay

Output lifecycle facts are typed events in the session truth log:

```text
OutputCandidateReceived
OutputDecodeFailed
OutputValidationStarted
OutputValidationRejected
OutputValidationDeferred
OutputAccepted
OutputCommitStarted
OutputCommitted
OutputPublished
OutputPublicationFailed
OutputFailed
```

`OutputState` is folded from these events. `RoleState` may cache the projection
but is not the truth source.

Replay remains a single forward scan. A compaction event may replace LLM message
history but must never discard committed run-output facts. Output events either
remain outside replacement history or are represented in a separate durable run
projection reconstructed alongside message history.

Unknown future output-event types remain ignorable under the existing session
forward-compatibility rule, while missing required commit facts fail closed when
recovering a typed result.

## 16. Accept, commit and publish

Successful output has three phases:

### Accept

Decode and all validation stages succeeded, producing an in-memory typed value.

### Commit

The canonical encoded value and provenance are durably appended:

```python
OutputCommitted(
    run_id=run_id,
    candidate_id=candidate_id,
    contract_id=contract_id,
    schema_version=schema_version,
    schema_fingerprint=schema_fingerprint,
    codec_version=codec_version,
    validator_versions=validator_versions,
    encoded_value=value,
)
```

Commit is the success linearization point and is exactly once per run.

### Publish

The committed result is projected to CLI, parent agent, RunGraph, AG-UI, ACP or
other consumers. Publication uses an outbox/event ID so delivery can be retried
and consumers can deduplicate it.

Publication failure must never repeat model calls, tools, validation or commit.

## 17. Unified run API

The target public abstraction is:

```python
agent: Agent[DepsT, OutputT]
result: RunResult[OutputT] = await agent.run(prompt, deps=deps)
```

```python
@dataclass(frozen=True)
class RunResult(Generic[OutputT]):
    run_id: str
    status: RunStatus
    output: OutputT
    output_record: CommittedOutput
    transcript: TranscriptRef
    usage: RunUsage
    provenance: OutputProvenance
```

Provenance includes at least:

```python
@dataclass(frozen=True)
class OutputProvenance:
    candidate_id: str
    model: str
    provider: str
    turn_id: str
    correction_attempts: int
    contract_id: str
    schema_fingerprint: str
    validator_versions: tuple[str, ...]
```

`Message` becomes a presentation/transcript concept rather than the universal
return type. Consumers use an `OutputPresenter` to turn a committed result into
text, blocks, cards or protocol events.

The migration must converge on this API. A temporary compatibility adapter may
exist during development, but permanent `run()` and `run_typed()` tracks are not
allowed.

## 18. RunGraph integration

RunGraph is the primary structured orchestration consumer.

Each graph declares a typed terminal contract in addition to its typed input and
state schemas. The graph's terminal node produces a `FinalCandidateAction` or a
typed graph result that passes through the same `OutputEngine`; it does not
invent a graph-only validation and completion subsystem.

Required RunGraph capabilities:

- graph input schema and graph output contract are independently versioned;
- terminal outputs may reference node results and channels through existing
  binding semantics;
- graph completion occurs only after the graph output is committed;
- a graph used as an agent tool may project its committed output into a
  `ToolResult.data`, while its own run still retains typed provenance;
- nested runs preserve parent/child run IDs and output provenance;
- graph resume reconstructs both graph frontier state and output state;
- graph retries cannot duplicate a committed terminal result;
- graph cycles and map/fold nodes remain unrelated to output correction budget.

This design lets RunGraph replace ad-hoc tools that prompt child agents for JSON
and parse their final text.

## 19. Provider capability negotiation

Extend `ModelProfile` with structured-output capabilities rather than scattered
provider checks:

```python
@dataclass(frozen=True)
class StructuredOutputCapabilities:
    modes: frozenset[OutputMode]
    schema_dialect: str
    supported_keywords: frozenset[str]
    supports_strict: bool
    supports_streaming: bool
    supports_root_array: bool
    supports_union: bool
    supports_recursive_schema: bool
    max_schema_bytes: int | None
```

An `OutputRepresentationNegotiator` combines contract requirements, model
profile and command protocol:

```python
class OutputRepresentationNegotiator:
    def negotiate(
        self,
        contract: OutputContract,
        profile: ModelProfile,
        protocol: CommandProtocol,
    ) -> OutputBinding: ...
```

Possible bindings:

```text
provider-native structured schema
semantic final action encoded as a provider tool
XML final block
prompted JSON
unsupported
```

Negotiation and every downgrade reason are explicit and observable. Providers
must not silently switch modes inside request methods.

Per-run schema guidance is dynamic context. It belongs below the system-prompt
cache boundary, normally through turn-context injection or provider request
parameters.

## 20. Retry and budget hierarchy

Output correction is distinct from all existing recovery budgets:

```python
@dataclass(frozen=True)
class RunBudgets:
    model_transport: RetryBudget
    provider_fallback: RetryBudget
    tool_execution: RetryBudget
    output_correction: RetryBudget
    validation_infrastructure: RetryBudget
    max_turns: int
    max_cost: Decimal
    deadline: datetime | None
```

Retry causes are typed:

```text
TransportFailure
ProviderFailure
ToolTransientFailure
OutputRejected
ValidatorUnavailable
```

Only `OutputRejected` consumes output-correction budget. A validator dependency
timeout consumes validation-infrastructure budget. Tool and provider failures do
not consume either.

Priority is:

```text
user cancellation
  > deadline
  > cost ceiling
  > output correction budget
  > subsystem retry budget
```

Every counter is event-sourced so crash/resume cannot reset a budget.

## 21. High-availability semantics

The guarantees are explicit:

- model requests are at least once and may be billed more than once after a
  boundary failure;
- pure validation is replayable;
- external-read validation is replayable but records observation provenance;
- output commit is exactly once per run;
- output publication is at least once with consumer deduplication;
- external tool effects retain the existing ledger's fail-closed semantics.

Cancellation behavior is phase-specific:

```text
before candidate        → cancelled
during pure validation  → cancelled; validation may safely replay on resume
after accept/before commit → resume validation/commit from durable facts
after commit             → run succeeded; only publication waiting is cancelled
```

Separate timeouts exist for decoding, each validator, the total validation
pipeline, model correction and publication. One global timeout must not obscure
which subsystem failed.

Distributed workers require run ownership leases and fencing tokens. A stale
worker must be unable to commit after another worker has acquired the run. The
commit record includes the fencing token checked by the durable backend.

## 22. Schema and validator evolution

Contract identity is explicit:

```python
@dataclass(frozen=True)
class OutputContractId:
    namespace: str
    name: str
    version: str
```

Every committed result records:

- contract ID and version;
- canonical schema fingerprint;
- codec name and version;
- semantic/policy validator names and versions.

Recovery rules:

- exact identity and fingerprint: decode and validate;
- registered source-to-target migration: migrate, then strictly validate;
- otherwise: fail closed with `OutputContractMismatch`.

```python
class OutputMigration(Protocol):
    source: OutputContractId
    target: OutputContractId

    def migrate(self, value: JsonValue) -> JsonValue: ...
```

Rollout data must not direct the runtime to import arbitrary modules/classes.
Migrations and contracts are registered by trusted application assembly.

## 23. Typed streaming

Streaming is an optional projection over an uncommitted candidate, never an
alternative acceptance path.

```text
provider deltas
  → binding-specific incremental parser
  → partial decoder
  → OutputSnapshotEvent
  → UI projection
  → final complete candidate
  → strict decode and full validation
  → commit
```

Rules:

- partial values are never committed;
- semantic validators do not run on incomplete snapshots by default;
- snapshots are debounced;
- final validation always starts from the complete raw candidate;
- a previously displayed snapshot can be invalidated;
- AG-UI/ACP receive snapshot IDs and an eventual committed/invalidated event.

## 24. Security boundaries

- Candidate values and schemas are untrusted inputs.
- Enforce schema size, nesting, recursion and candidate byte limits.
- Disable remote JSON Schema references.
- Schema transformations are pure functions over bounded data.
- Prompted JSON parsing never evaluates code.
- Validators receive explicit narrow dependencies, never `Role`, environment or
  memory wholesale.
- Output values are redacted in traces by default; sensitive fields may carry
  field-level classification.
- Contract/migration lookup uses trusted registries, not rollout imports.
- Accepting output never authorizes commands embedded inside that output.
- Publication adapters independently enforce destination-specific data policy.

## 25. Observability

Output processing emits typed events and spans:

```text
run
  └── model_turn
       └── output_candidate
            ├── decode
            ├── validate:structural
            ├── validate:semantic
            ├── validate:policy
            ├── commit
            └── publish
```

Metrics derived from events include:

- candidates per run;
- decode and validation rejection rates;
- correction success rate;
- attempts to acceptance;
- per-validator latency and availability;
- commit latency;
- publication retry count;
- contract mismatch count;
- representation downgrade rate;
- output size distribution.

No inline logging is required; subscribers and `@log_class` follow the existing
logging convention.

## 26. Layering and module placement

The dependency direction remains unchanged. Low layers receive high-level
capabilities only through `common/interface` protocols.

Target module layout:

```text
common/schema/
  run.py                 # IDs, RunStatus, RunResult, provenance
  action.py              # ModelTurn and AgentAction union
  output.py              # contract IDs, schema documents, output records
  validation.py          # decisions, issues and feedback data

common/interface/
  output_decoder.py
  output_validator.py
  completion_policy.py
  output_presenter.py
  run_store.py

common/exception/
  output.py

common/events/
  output.py

parser/
  output_binding.py       # wire binding ABC
  output_negotiator.py
  native_output.py
  xml_output.py
  prompted_output.py

loop/
  action_dispatcher.py
  completion.py
  run_machine.py

roles/
  output_contract.py      # trusted assembly and concrete codecs
  output_engine.py
  run_context.py
  output_presenter.py

executor/tasks/bggraph/
  output.py               # RunGraph terminal contract integration

session/
  output_replay.py
  output_migration.py
  outbox.py
```

Exact filenames may be adjusted to existing package conventions, but these
boundaries are not negotiable:

```text
wire binding
  ≠ semantic action
  ≠ validation
  ≠ durable commit
  ≠ presentation
```

## 27. Testing strategy

### Codec contract suite

Every decoder runs the same conformance tests:

- encode/decode round trip;
- invalid input rejection;
- canonical schema and stable fingerprint;
- bounded size/depth handling;
- union, root list and primitive behavior;
- migration into the current contract.

### Wire-binding conformance suite

Every provider/protocol binding runs:

- text output;
- structured object and root array;
- union output;
- invalid field feedback;
- multiple candidates;
- candidate plus ordinary tool;
- stream interruption;
- correction feedback;
- unsupported-schema downgrade.

### State-machine property tests

Generated event sequences verify:

- at most one commit per run;
- publish only after commit;
- rejected candidates never commit without a new candidate;
- replay fold equals uninterrupted state;
- budgets never reset or go negative;
- cancellation prevents subsequent actions;
- contract mismatch never silently decodes.

### Fault injection

Inject process failure before and after:

- candidate recording;
- decode;
- each validation stage;
- accept;
- commit fsync;
- outbox append;
- publication acknowledgment.

### Deterministic model simulation

Provide a public `ScriptedModel` capable of returning semantic turns:

```python
ScriptedModel([
    ModelTurn(actions=(FinalCandidateAction(raw=invalid),)),
    ModelTurn(actions=(FinalCandidateAction(raw=valid),)),
])
```

Tests should not construct provider SDK response mocks unless testing a wire
binding itself.

### RunGraph integration suite

- typed graph terminal result;
- invalid terminal candidate correction;
- graph crash/resume before and after commit;
- nested graph/agent provenance;
- graph used as tool projects committed value to `ToolResult.data` once;
- graph cycles do not consume output-correction budget;
- parent cancellation dominates child output correction.

## 28. Delivery phases

### Phase 0 — ADR and architecture guards

- Record this decision as the output/run architecture ADR.
- Add forbidden-import and invariant tests.
- Define session/run/turn/attempt/action/candidate identities.
- Freeze event naming and canonical JSON rules.

### Phase 1 — Semantic turn model

- Introduce `ModelTurn` and `AgentAction`.
- Normalize current native and XML responses into actions.
- Add `CompletionPolicy` and `ActionDispatcher` protocols.
- Preserve current user-visible behavior through a text-output contract.
- Remove direct loop dependence on `tool_calls == []` as completion truth.

### Phase 2 — Unified run model

- Introduce run-scoped context and IDs.
- Introduce `Agent[DepsT, OutputT]` and `RunResult[OutputT]`.
- Make text output use the same contract path.
- Convert CLI/environment consumers to presenters.
- Remove `last_end_output` as a cross-component result channel.
- Keep any old return adapter explicitly temporary and delete it in this phase.

### Phase 3 — Output engine and validation

- Implement schema documents, codecs and contracts.
- Implement validation decisions and correction feedback.
- Implement the output state machine and correction budget.
- Encode semantic final actions as provider tools only in wire bindings.
- Enforce final-candidate action exclusivity.

### Phase 4 — Durable commit and outbox

- Add output lifecycle events and replay fold.
- Implement accept/commit/publish separation.
- Add exactly-once commit and idempotent publication outbox.
- Integrate JSONL and Temporal durable paths.
- Add crash fault-injection coverage.

### Phase 5 — RunGraph integration

- Add graph terminal output contracts.
- Route graph completion through `OutputEngine`.
- Persist graph output provenance and nested run identity.
- Project committed graph results into tool results when a graph is exposed as a
  tool.
- Remove graph-local prompt/JSON completion conventions made redundant by the
  shared output engine.

### Phase 6 — Representation negotiation

- Add structured-output capability profiles.
- Add explicit binding negotiation and downgrade reporting.
- Implement native-schema, semantic-tool, XML and prompted bindings.
- Add provider/protocol conformance suites.

### Phase 7 — Typed streaming

- Add partial parsers, snapshots and invalidation.
- Map snapshots and commits to AG-UI/ACP.
- Preserve strict complete-candidate validation as the sole commit path.

### Phase 8 — Schema evolution and distributed HA

- Add contract and validator version registries.
- Add explicit migrations.
- Add worker leases and fencing-token commit checks.
- Exercise concurrent resume, stale worker and publication recovery scenarios.

## 29. Definition of done

The architecture is complete when:

- every run, including text runs, has an explicit output contract;
- provider adapters emit semantic actions and the loop is wire-agnostic;
- final output never executes as an ordinary tool;
- `RunResult[T]` is the single public completion result;
- RunGraph uses the common output engine for typed terminal results;
- successful completion means exactly one durable output commit;
- crash at any output boundary replays without duplicate tools, validation side
  effects, commits or consumer-visible results;
- schema/version mismatch is explicit and migration-controlled;
- all provider bindings pass the same conformance suite;
- output correction, provider recovery, tool retry and validation-infrastructure
  retry have independent durable budgets;
- temporary compatibility adapters and obsolete result-parsing paths are removed.

