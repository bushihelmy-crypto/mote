#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Shared fixtures and duck-typed fakes for the ExecutionEngine test suite.

The engine is deliberately
role-agnostic: it drives a handful of injected collaborators behind narrow
interfaces and reads/writes the shared ``active`` signal via two plain
callables. None of those collaborators need a network, so every one is
duck-typed here and the tests stay fully offline.

Collaborators the flow touches (and the slice each fake implements):

- ``inference_engine``  : ``async start(...)`` + ``result.content`` + ``async join()``
  (see :class:`FakeThinkEngine`).
- ``command_channel``: ``iter_commands(engine, valid_names)`` (async gen),
  ``record_turn(memory, content, executed)`` and ``is_terminal(engine)``
  (see :class:`FakeChannel`).
- ``executor``      : ``async run_command(name, args, result_id=None)`` returning
  a :class:`FakeResult` (``output`` / ``success`` / structured ``media``).
- ``memory``        : a tiny ``MessageStore`` (``get`` / ``add`` / ``add_batch``).
- ``context_provider``: ``execution_context()`` / ``async prepare()`` /
  ``async resolve_llm(messages)`` (see :class:`FakeContextProvider`).
- ``is_active`` / ``set_active`` : read/write a shared bool holder.
- ``get_bg_pool``  : returns a :class:`FakeBgPool` or ``None``.

``make_engine`` wires a fully-defaulted engine and hands back the engine and the
fakes so a test can pre-seed inputs and assert on recorded calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional

import pytest

from mote.contracts.conversation import Message, UserMessage
from mote.contracts.model.inference import EndpointCapabilitySnapshot, InferenceTargetLease, ResolvedInferenceTarget
from mote.contracts.tool.catalog import (
    MaterializedToolCatalog,
    MaterializedToolDefinition,
    ToolBindingSnapshot,
    ToolCatalogIdentity,
    ToolDispatchResult,
)
from mote.kernel.commands.contracts import ExecutedCommand, HistoryProjection
from mote.kernel.execution import PROCEED, BudgetVerdict, ExecutionContext, ExecutionEngine
from mote.kernel.execution.request import InferenceRequest
from mote.product.config.model_checkpoint import approved_model_checkpoint_policy
from mote.runtime.durable.inference_checkpoint import InferenceCheckpoint
from mote.runtime.models.session_projection import ModelSessionProjectionStore
from mote.runtime.persistence.execution_transaction import RuntimeExecutionTransaction
from mote.runtime.session.workspace import SessionWorkspace

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Result:
    """Minimal stand-in for a finished InferenceResult (only ``content`` read)."""

    def __init__(self, content: str = ""):
        self.content = content


class _NoModelCallRecovery:
    def inspect_recovery(self, model_call_id: str):
        from mote.contracts.ports.model.recovery import ModelRecoveryDisposition, ModelRecoveryInspection

        return ModelRecoveryInspection(model_call_id, ModelRecoveryDisposition.ABSENT)


class _NoArtifactResolver:
    async def resolve(self, ref, policy):
        raise AssertionError("flow fake does not externalize Model output")


class FakeInferenceCheckpoint:
    def __init__(self):
        from mote.contracts.execution.models import InferenceCheckpointState

        self.state = InferenceCheckpointState(
            model_call_id="flow-model-call",
            inference_attempt_id="flow-attempt",
            inference_fencing_token=1,
        )

    async def reinstate(self):
        return False

    def resume(self):
        return None

    def begin_call(self, state):
        self.state = state

    def refresh(self, state):
        self.state = state

    def mark_wire_started(self):
        return None

    def discard(self):
        return None

    def abort(self):
        return None

    async def prepare_consumption(self, operation_id):
        from mote.contracts.events.model import InferenceCheckpointConsumedEvent

        state = self.state
        return InferenceCheckpointConsumedEvent(
            state.model_call_id,
            state.inference_attempt_id,
            state.inference_fencing_token,
            operation_id,
        )

    def acknowledge_consumption(self, event):
        return None


class FakeSessionFactSink:
    async def commit_facts(self, events):
        self.events = events

    async def commit_fact(self, event):
        await self.commit_facts((event,))


class FakeThinkEngine:
    """Duck-typed BaseInferenceEngine exposing only what the loop reads."""

    def __init__(self, *, content: str = ""):
        self.result = _Result(content)
        self.start_calls: list[dict] = []
        self.join_calls = 0
        self._done = False
        self.model_call_id = "fake-model-call"
        # Results adopted via the durable reinstate path (skip-the-LLM resume).
        self.reinstated: list = []

    async def start(
        self,
        req,
        system_prompt,
        tool_specs=None,
        *,
        target,
        model_call_id,
        resume=False,
        output_binding=None,
        output_schema=None,
        output_run_id="",
        schema_fingerprint="",
        attempt=None,
        **_kwargs,
    ):
        self._done = False
        self.model_call_id = model_call_id
        self.start_calls.append(
            {
                "req": req,
                "system_prompt": system_prompt,
                "tool_specs": tool_specs,
                "target": target,
                "model_call_id": model_call_id,
                "resume": resume,
            }
        )

    def reinstate(self, result) -> None:
        # Mirror InferenceEngine.reinstate: adopt the journal-recovered result and
        # mark done (no task) so the loop reads it without re-paying the model.
        self.result = result
        self.reinstated.append(result)
        self._done = True

    async def join(self) -> None:
        self.join_calls += 1
        self._done = True

    @property
    def done(self) -> bool:
        return self._done


class FakeChannel:
    """Duck-typed CommandChannel.

    ``commands`` is the list ``iter_commands`` yields (command IR dicts shaped
    ``{"id", "command_name", "args"}``). ``terminal`` drives ``is_terminal``.
    ``record_turn`` records ``(content, executed)`` tuples for assertion.
    """

    def __init__(self, *, commands: Optional[list[dict]] = None, terminal: bool = False):
        self.commands = list(commands or [])
        self.terminal = terminal
        self.recorded_turns: list[tuple[str, list[dict]]] = []
        # Two-phase (checkpoint) recording: the assistant call recorded ahead of
        # execution, then the results after. Populated only on the EXTERNAL path.
        self.recorded_calls: list[tuple[str, list[ExecutedCommand]]] = []
        self.recorded_results: list[list[ExecutedCommand]] = []
        self.iter_calls: list[set] = []
        self.output_feedback = []

    async def iter_commands(self, inference_engine, valid_names):
        self.iter_calls.append(set(valid_names))
        for cmd in self.commands:
            yield cmd

    async def record_turn(self, memory, content, executed) -> None:
        self.recorded_turns.append((content, list(executed)))

    async def project_call(self, content, executed):
        self.recorded_calls.append((content, [replace(entry) for entry in executed]))
        return HistoryProjection((), f"call:{content}")

    async def project_results(self, executed):
        self.recorded_results.append(list(executed))
        return HistoryProjection((), "results")

    async def project_output_candidate(self, content, candidate, *, accepted, feedback=None):
        self.recorded_turns.append((content, []))
        if feedback is not None:
            self.output_feedback.append(feedback)
        return HistoryProjection((), f"output:{candidate.candidate_id}")

    async def project_turn(self, content, executed):
        self.recorded_turns.append((content, list(executed)))
        return HistoryProjection((), f"turn:{content}")

    async def record_call(self, memory, content, executed) -> None:
        # Snapshot at call time (before bodies run) so a test can prove the
        # assistant message was recorded ahead of execution.
        self.recorded_calls.append((content, list(executed)))

    async def record_results(self, memory, executed) -> None:
        self.recorded_results.append(list(executed))

    async def record_output_feedback(self, memory, feedback) -> None:
        self.output_feedback.append(feedback)

    async def record_output_candidate(self, memory, content, candidate, *, accepted, feedback=None) -> None:
        self.recorded_turns.append((content, []))
        if feedback is not None:
            self.output_feedback.append(feedback)

    async def model_turn(self, result):
        from mote.contracts.model.turn import FinalCandidateAction, ModelTurn, TextAction, ToolCallAction

        content = result.content or ""
        actions = [TextAction(content=content)] if content else []
        actions.extend(
            ToolCallAction(
                action_id=command.get("id") or "",
                name=command["command_name"],
                arguments=command.get("args") or {},
            )
            for command in self.commands
        )
        if self.terminal:
            actions.append(FinalCandidateAction(raw=content, representation="test"))
        return ModelTurn(content=content, actions=actions)

    def react_result(self, outputs: str) -> str:
        # Mirror the base default (plain outputs); the loop's content is then
        # assertable against the joined command outputs / no-commands notice.
        return outputs


@dataclass
class FakeResult:
    """Stand-in for the executor's ToolResult return."""

    output: str = "ok"
    success: bool = True
    media: list = field(default_factory=list)
    artifacts: list = field(default_factory=list)
    file_changes: list = field(default_factory=list)
    terminate: bool = False
    retention: str | None = None
    resource_path: str | None = None
    payload: object | None = None
    execution_value: object | None = None


class FakeExecutor:
    """Duck-typed BaseToolExecutor.

    ``results`` maps a command name to the :class:`FakeResult` ``run_command``
    returns; anything missing falls back to ``default``.
    """

    def __init__(
        self,
        *,
        results: Optional[dict[str, FakeResult]] = None,
        default: Optional[FakeResult] = None,
        external: Optional[set[str]] = None,
    ):
        self.results = results or {}
        self.default = default or FakeResult()
        self.calls: list[dict] = []
        self.external = set(external or ())

    async def run_command(self, name, args, result_id=None):
        self.calls.append({"name": name, "args": args, "result_id": result_id})
        return self.results.get(name, self.default)


class FakeToolExecutionPort:
    def __init__(self, executor):
        self.executor = executor
        self.approval = None
        self.fileops_transactions = {}

    def bind_approval_coordinator(self, coordinator):
        self.approval = coordinator

    def bind_fileops_transaction(self, request, transaction_id):
        self.fileops_transactions[request.call_id] = transaction_id

    async def authorize(self, request):
        return ToolDispatchResult(True)

    async def dispatch(self, request):
        result = await self.executor.run_command(
            request.tool_name,
            request.arguments,
            result_id=request.call_id or None,
        )
        return ToolDispatchResult(True, value=result)

    def release(self, snapshot):
        return True

    def invocation_identity(self, request):
        from mote.contracts.tool import (
            ToolAttemptOrdinal,
            ToolInvocationId,
            ToolInvocationIdentity,
            tool_arguments_digest,
        )

        return ToolInvocationIdentity(
            ToolInvocationId(request.call_id),
            ToolAttemptOrdinal(1),
            f"{request.tool_name}@1",
            request.registry_revision,
            tool_arguments_digest(request.arguments),
            "fake-session",
            "fake-run",
        )


class FakeMemory:
    """Minimal in-memory MessageStore stand-in."""

    def __init__(self, messages: Optional[list[Message]] = None):
        self.messages: list[Message] = list(messages or [])
        self.add_batch_calls: list[list[Message]] = []

    def get(self, k: int = 0) -> list[Message]:
        if k <= 0:
            return list(self.messages)
        return self.messages[-k:]

    async def add(self, message: Message) -> None:
        self.messages.append(message)

    async def add_batch(self, messages) -> None:
        msgs = [m for m in messages if m is not None]
        self.add_batch_calls.append(list(msgs))
        self.messages.extend(msgs)

    def apply_committed_messages(self, messages) -> None:
        self.messages.extend(messages)


class FakeLLM:
    """Narrow fake whose ``aask`` behavior is exercised by the loop."""

    def __init__(self, reply: str = "llm-question"):
        self.reply = reply
        self.aask_calls: list = []

    async def aask(self, msg, *args, **kwargs):
        self.aask_calls.append(msg)
        return self.reply


class FakeContextProvider:
    """Duck-typed BaseContextProvider.

    Hands back the static :class:`ExecutionContext`, a canned :class:`InferenceRequest`
    per ``prepare()`` and a :class:`FakeLLM` per ``resolve_llm()``.
    """

    def __init__(
        self,
        ctx: ExecutionContext,
        *,
        think_request: Optional[InferenceRequest] = None,
        llm: Optional[FakeLLM] = None,
    ):
        self._ctx = ctx
        from mote.contracts.output import OutputRepresentationCapabilities
        from mote.kernel.output.binding import negotiate_output_binding

        self._think_request = think_request or InferenceRequest(
            req=[UserMessage("hi")],
            system_prompt="sys",
            tool_specs=["spec"],
            output_binding=negotiate_output_binding(
                is_text=True,
                capabilities=OutputRepresentationCapabilities(supports_text=True, protocol="fake"),
            ),
            command_channel=None,
            output_schema={},
            schema_fingerprint="fake-schema",
        )
        self.llm = ResolvedInferenceTarget(
            route_id="fake",
            command_protocol="native",
            command_protocol_version="1",
            capabilities=EndpointCapabilitySnapshot(supports_resume=True),
            capability_fingerprint="fake-capabilities",
            projection_compatibility_key="fake-projection",
            lease=InferenceTargetLease("fake-target", "fake-lease", 99999999999.0),
        )
        self.prepare_calls = 0
        self.resolve_calls: list = []
        # The verdict enforce_budget() returns. Defaults to PROCEED (no cap);
        # tests set a stop verdict to exercise the loop's budget gate.
        self.budget_verdict: BudgetVerdict = PROCEED
        self.enforce_budget_calls = 0

    def execution_context(self) -> ExecutionContext:
        return self._ctx

    async def prepare(self) -> InferenceRequest:
        self.prepare_calls += 1
        return self._think_request

    async def resolve_inference_target(self, messages=None, **_kwargs):
        self.resolve_calls.append(messages)
        return self.llm

    def resolve_task_model_route(self, task):
        return self.llm

    def finalize_for_model(self, request, target):
        if request.command_channel is None:
            request.command_channel = getattr(self, "channel", None)
        request.tool_snapshot = self.tool_snapshot
        return request

    async def enforce_budget(self) -> BudgetVerdict:
        self.enforce_budget_calls += 1
        return self.budget_verdict


class FakeBgPool:
    """Duck-typed BackgroundPool.

    ``pending`` is the number of times ``has_pending()`` should report busy;
    each pin snapshot delivers one inbox notification (so a parked loop drains).
    """

    def __init__(self, pending: int = 0):
        self.pending = pending
        self.wait_calls = 0
        self.buffer = None
        self.owner = object()

    def has_pending(self) -> bool:
        return self.pending > 0

    @property
    def pending_count(self) -> int:
        return self.pending

    def pin_snapshot(self, *, owner):
        from types import SimpleNamespace

        from mote.contracts.conversation import UserMessage

        pin_count = self.pending
        if self.pending > 0:
            self.pending -= 1
            self.wait_calls += 1
            assert self.buffer is not None
            self.buffer.push(UserMessage("background task completed"))
        return SimpleNamespace(pin_count=pin_count)


class FakeOutputEngine:
    run_id = "fake-output-run"

    def __init__(self, *, accepted: bool = True):
        self.accepted = accepted
        self.candidates = []
        self.commit_calls = 0
        self.staged_output = None
        self.validated_candidate = None
        self.committed_output = None

    @property
    def has_restored_terminal_output(self):
        return False

    async def evaluate(self, candidate):
        from mote.contracts.output import OutputEvaluation, ValidatedCandidate

        self.candidates.append(candidate)
        candidate_id = candidate.candidate_id or "fake"
        if self.accepted:
            self.validated_candidate = ValidatedCandidate(
                candidate_id,
                "mote.text@1",
                "sha",
                candidate.raw,
                candidate.raw,
            )
        return OutputEvaluation(
            accepted=self.accepted,
            candidate_id=candidate_id,
            value=candidate.raw if self.accepted else None,
        )

    async def commit_final(self, message, *, companion_facts=(), fact_sink=None):
        from mote.contracts.output import CommittedOutput

        self.commit_calls += 1
        assert self.validated_candidate is not None
        self.committed_output = CommittedOutput(
            self.validated_candidate.candidate_id,
            "mote.text@1",
            "sha",
            self.validated_candidate.value,
        )
        return self.committed_output


# ---------------------------------------------------------------------------
# Builders / fixtures
# ---------------------------------------------------------------------------


def make_flow_context(**overrides) -> ExecutionContext:
    """Build a :class:`ExecutionContext` with sensible test defaults."""
    from mote.contracts.conversation import MessageQueue

    params: dict[str, Any] = dict(
        name="Alice",
        display_name="Alice(Tester)",
        tools=[
            "Read",
            "Search",
            "Glob",
            "Grep",
            "Bash",
            "RunGraph",
            "End",
            "AskUserQuestion",
        ],
        msg_buffer=MessageQueue(),
        watch=set(),
        enable_memory=True,
        observe_all=True,
    )
    params.update(overrides)
    return ExecutionContext(**params)


@dataclass
class FlowBundle:
    """The engine plus every fake wired into it (for inspection in tests)."""

    engine: ExecutionEngine
    ctx: ExecutionContext
    inference_engine: FakeThinkEngine
    channel: FakeChannel
    executor: FakeExecutor
    memory: FakeMemory
    provider: FakeContextProvider
    active: list  # single-element bool holder
    bg_pool_holder: list  # single-element [FakeBgPool | None]
    reported: list  # think results published via report_inference_result

    @property
    def buffer(self):
        return self.ctx.msg_buffer


@pytest.fixture
def make_engine(tmp_path):
    """Factory: build a fully wired :class:`ExecutionEngine` and its fakes.

    Pass keyword overrides for any collaborator or for ``ExecutionContext`` fields
    (anything not a known collaborator is forwarded to ``make_flow_context``).
    """

    def _factory(
        *,
        ctx: Optional[ExecutionContext] = None,
        inference_engine: Optional[FakeThinkEngine] = None,
        channel: Optional[FakeChannel] = None,
        executor: Optional[FakeExecutor] = None,
        memory: Optional[FakeMemory] = None,
        provider: Optional[FakeContextProvider] = None,
        active: bool = True,
        bg_pool: Optional[FakeBgPool] = None,
        turn_context_bus=None,
        get_cwd: Optional[Callable[[], str]] = None,
        output_engine=None,
        graph_builder=None,
        drain_writes=None,
        **ctx_overrides,
    ) -> FlowBundle:
        ctx = ctx or make_flow_context(**ctx_overrides)
        if bg_pool is not None:
            bg_pool.buffer = ctx.msg_buffer
        inference_engine = inference_engine or FakeThinkEngine()
        channel = channel or FakeChannel()
        executor = executor or FakeExecutor()
        memory = memory or FakeMemory()
        provider = provider or FakeContextProvider(ctx)
        provider.channel = channel
        output_engine = output_engine or FakeOutputEngine()
        definitions = tuple(
            MaterializedToolDefinition(
                name,
                "",
                {"type": "object", "properties": {}},
                f"{name}@1",
                "external" if name in executor.external else "pure",
            )
            for name in ctx.tools
        )
        provider.tool_snapshot = ToolBindingSnapshot(
            "fake-snapshot",
            "fake-application-generation",
            MaterializedToolCatalog(ToolCatalogIdentity("fake", "1"), 1, definitions, "fake"),
            "fake-target",
            "fake-capabilities",
            "fake-provider",
            1,
            "fake-lease",
        )

        active_holder = [active]
        bg_holder = [bg_pool]
        reported: list = []
        accepted_pending_acts: list = []
        settled_pending_acts: list = []
        external_effects: list = []

        def is_active() -> bool:
            return active_holder[0]

        def set_active(v: bool) -> None:
            active_holder[0] = v

        def get_bg_pool():
            return bg_holder[0]

        def report_inference_result(result) -> None:
            reported.append(result)

        engine_kwargs = {}
        if graph_builder is not None:
            engine_kwargs["graph_builder"] = graph_builder
        checkpoint = FakeInferenceCheckpoint()

        async def default_drain():
            return None

        transaction = RuntimeExecutionTransaction(
            run_id=output_engine.run_id,
            fencing_token=1,
            memory=memory,
            output_engine=output_engine,
            inference_checkpoint=checkpoint,
            session_fact_sink=FakeSessionFactSink(),
            drain_writes=drain_writes or default_drain,
        )

        class FakePendingActAcceptance:
            async def accept(self, actions, snapshot, messages):
                from types import SimpleNamespace

                from mote.contracts.execution.pending_act import PendingAction
                from mote.contracts.tool import ToolEffect, ToolInvocationId

                accepted_pending_acts.append((actions, snapshot, messages))
                pending = tuple(
                    PendingAction(
                        ordinal,
                        ToolInvocationId(action.action_id),
                        action.action_id,
                        action.name,
                        f"{action.name}@1",
                        snapshot.registry_revision,
                        ToolEffect(
                            next(item.effect for item in snapshot.catalog.definitions if item.name == action.name)
                        ),
                        0,
                    )
                    for ordinal, action in enumerate(actions)
                )
                return SimpleNamespace(
                    frontier=SimpleNamespace(
                        frontier_id=SimpleNamespace(value="fake-frontier"),
                        model_call_id="fake-model-call",
                        actions=pending,
                    )
                )

            async def settle(
                self,
                acceptance,
                messages,
                *,
                continue_inference,
                effect_receipts=(),
                action_results=(),
                skipped=None,
                rejected_approval_request_id=None,
            ):
                settled_pending_acts.append(
                    (
                        acceptance,
                        messages,
                        continue_inference,
                        effect_receipts,
                        action_results,
                    )
                )

            def resume(self, frontier, snapshot):
                raise AssertionError("flow fake has no recovered PendingAct")

            async def begin_external_effect(self, acceptance, ordinal, identity):
                from mote.contracts.ports.execution.pending_act import ExternalEffectPermit

                external_effects.append(("started", identity))
                return ExternalEffectPermit(getattr(acceptance, "frontier", acceptance), identity)

            async def begin_invoke(self, acceptance, ordinal, identity):
                from mote.contracts.execution.pending_act_claim import PendingActClaimId, PendingActInvokePermit

                return PendingActInvokePermit(
                    PendingActClaimId("fake-claim"),
                    acceptance.frontier.frontier_id,
                    "fake-owner",
                    "fake-incarnation",
                    0,
                    1,
                    getattr(acceptance.frontier, "revision", 0),
                    identity.invocation_id,
                    acceptance.frontier.actions[ordinal].fileops_transaction_id,
                )

            async def mark_external_effect_in_doubt(self, permit, *, evidence):
                external_effects.append(("in_doubt", permit.identity, evidence))

            async def resolve_approval(self, acceptance, intent):
                from mote.contracts.ports.tool.approval import ToolApprovalResolution

                return ToolApprovalResolution(True)

        class FakeExecutionRestore:
            def snapshot(self):
                from mote.contracts.execution.restore import NoPendingExecution

                return NoPendingExecution()

        pending_act_port = FakePendingActAcceptance()
        engine = ExecutionEngine(
            inference_engine=inference_engine,
            command_channel=channel,
            executor=executor,
            tool_execution_port=FakeToolExecutionPort(executor),
            memory=memory,
            context_provider=provider,
            is_active=is_active,
            set_active=set_active,
            get_bg_pool=get_bg_pool,
            report_inference_result=report_inference_result,
            inference_checkpoint=checkpoint,
            execution_transaction=transaction,
            pending_act_acceptance=pending_act_port,
            execution_restore=FakeExecutionRestore(),
            turn_context_bus=turn_context_bus,
            get_cwd=get_cwd,
            output_engine=output_engine,
            **engine_kwargs,
        )
        engine._tool_snapshot = provider.tool_snapshot
        engine.accepted_pending_acts = accepted_pending_acts
        engine.settled_pending_acts = settled_pending_acts
        engine.pending_act_port = pending_act_port
        engine.external_effects = external_effects
        return FlowBundle(
            engine=engine,
            ctx=ctx,
            inference_engine=inference_engine,
            channel=channel,
            executor=executor,
            memory=memory,
            provider=provider,
            active=active_holder,
            bg_pool_holder=bg_holder,
            reported=reported,
        )

    return _factory
