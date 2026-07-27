#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Shared fixtures and duck-typed fakes for the AgentFlowEngine test suite.

The engine is deliberately
role-agnostic: it drives a handful of injected collaborators behind narrow
interfaces and reads/writes the shared ``active`` signal via two plain
callables. None of those collaborators need a network, so every one is
duck-typed here and the tests stay fully offline.

Collaborators the flow touches (and the slice each fake implements):

- ``think_engine``  : ``async start(...)`` + ``result.content`` + ``async join()``
  (see :class:`FakeThinkEngine`).
- ``command_channel``: ``iter_commands(engine, valid_names)`` (async gen),
  ``record_turn(memory, content, executed)`` and ``is_terminal(engine)``
  (see :class:`FakeChannel`).
- ``executor``      : ``async run_command(name, args, result_id=None)`` returning
  a :class:`FakeResult` (``output`` / ``success`` / structured ``media``).
- ``memory``        : a tiny ``MessageStore`` (``get`` / ``add`` / ``add_batch``).
- ``context_provider``: ``flow_context()`` / ``async prepare()`` /
  ``async resolve_llm(messages)`` (see :class:`FakeContextProvider`).
- ``is_active`` / ``set_active`` : read/write a shared bool holder.
- ``get_bg_pool``  : returns a :class:`FakeBgPool` or ``None``.

``make_engine`` wires a fully-defaulted engine and hands back the engine and the
fakes so a test can pre-seed inputs and assert on recorded calls.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import pytest

from mote.contracts.schema import Message, UserMessage
from mote.kernel.flow import PROCEED, AgentFlowEngine, BudgetVerdict, FlowContext
from mote.runtime.agent.context_provider.request import ThinkRequest

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Result:
    """Minimal stand-in for a finished ThinkResult (only ``content`` read)."""

    def __init__(self, content: str = ""):
        self.content = content


class FakeThinkEngine:
    """Duck-typed BaseThinkEngine exposing only what the loop reads."""

    def __init__(self, *, content: str = ""):
        self.result = _Result(content)
        self.start_calls: list[dict] = []
        self.join_calls = 0
        # Results adopted via the durable reinstate path (skip-the-LLM resume).
        self.reinstated: list = []

    async def start(
        self,
        req,
        system_prompt,
        tool_specs=None,
        *,
        model_route,
        model_call_id,
        duplicate_route=None,
        resume=False,
        output_binding=None,
        output_schema=None,
        output_run_id="",
        schema_fingerprint="",
    ):
        self.start_calls.append(
            {
                "req": req,
                "system_prompt": system_prompt,
                "tool_specs": tool_specs,
                "model_route": model_route,
                "model_call_id": model_call_id,
                "duplicate_route": duplicate_route,
                "resume": resume,
            }
        )

    def reinstate(self, result) -> None:
        # Mirror ThinkEngine.reinstate: adopt the journal-recovered result and
        # mark done (no task) so the loop reads it without re-paying the model.
        self.result = result
        self.reinstated.append(result)

    async def join(self) -> None:
        self.join_calls += 1


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
        self.recorded_calls: list[tuple[str, list[dict]]] = []
        self.recorded_results: list[list[dict]] = []
        self.iter_calls: list[set] = []
        self.output_feedback = []

    async def iter_commands(self, think_engine, valid_names):
        self.iter_calls.append(set(valid_names))
        for cmd in self.commands:
            yield cmd

    async def record_turn(self, memory, content, executed) -> None:
        self.recorded_turns.append((content, list(executed)))

    async def record_call(self, memory, content, executed) -> None:
        # Snapshot at call time (before bodies run) so a test can prove the
        # assistant message was recorded ahead of execution.
        self.recorded_calls.append((content, [dict(e) for e in executed]))

    async def record_results(self, memory, executed) -> None:
        self.recorded_results.append([dict(e) for e in executed])

    async def record_output_feedback(self, memory, feedback) -> None:
        self.output_feedback.append(feedback)

    async def record_output_candidate(self, memory, content, candidate, *, accepted, feedback=None) -> None:
        self.recorded_turns.append((content, []))
        if feedback is not None:
            self.output_feedback.append(feedback)

    async def model_turn(self, think_engine):
        from mote.contracts.model_actions import FinalCandidateAction, ModelTurn, TextAction, ToolCallAction

        content = think_engine.result.content or ""
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
    terminate: bool = False
    retention: str | None = None
    resource_path: str | None = None
    # Structured payload the loop forwards onto the executed entry (only
    # SearchTools' {tool_references} is read downstream; None for every other
    # tool). Mirrors ToolResult.data.
    data: Any = None


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
        ledgered: Optional[set[str]] = None,
    ):
        self.results = results or {}
        self.default = default or FakeResult()
        self.calls: list[dict] = []
        # Names the fake would EXTERNAL-ledger; drives will_ledger so a test can
        # exercise the loop's pre-execution checkpoint path.
        self.ledgered = set(ledgered or ())

    async def run_command(self, name, args, result_id=None):
        self.calls.append({"name": name, "args": args, "result_id": result_id})
        return self.results.get(name, self.default)

    def will_ledger(self, name, args, result_id) -> bool:
        return result_id is not None and name in self.ledgered


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


class FakeLLM:
    """Duck-typed LLMClient — only ``aask`` is exercised by the loop."""

    def __init__(self, reply: str = "llm-question"):
        self.reply = reply
        self.aask_calls: list = []

    async def aask(self, msg, *args, **kwargs):
        self.aask_calls.append(msg)
        return self.reply


class FakeContextProvider:
    """Duck-typed BaseContextProvider.

    Hands back the static :class:`FlowContext`, a canned :class:`ThinkRequest`
    per ``prepare()`` and a :class:`FakeLLM` per ``resolve_llm()``.
    """

    def __init__(
        self,
        ctx: FlowContext,
        *,
        think_request: Optional[ThinkRequest] = None,
        llm: Optional[FakeLLM] = None,
    ):
        self._ctx = ctx
        from mote.contracts.output import OutputRepresentationCapabilities
        from mote.kernel.output_binding import negotiate_output_binding

        self._think_request = think_request or ThinkRequest(
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
        self.llm = llm or FakeLLM()
        self.prepare_calls = 0
        self.resolve_calls: list = []
        # The verdict enforce_budget() returns. Defaults to PROCEED (no cap);
        # tests set a stop verdict to exercise the loop's budget gate.
        self.budget_verdict: BudgetVerdict = PROCEED
        self.enforce_budget_calls = 0

    def flow_context(self) -> FlowContext:
        return self._ctx

    async def prepare(self) -> ThinkRequest:
        self.prepare_calls += 1
        return self._think_request

    async def resolve_model_route(self, messages=None):
        self.resolve_calls.append(messages)
        return self.llm

    def resolve_task_model_route(self, task):
        return self.llm

    def finalize_for_model(self, request, route):
        if request.command_channel is None:
            request.command_channel = getattr(self, "channel", None)
        return request

    async def enforce_budget(self) -> BudgetVerdict:
        self.enforce_budget_calls += 1
        return self.budget_verdict


class FakeBgPool:
    """Duck-typed BackgroundPool.

    ``pending`` is the number of times ``has_pending()`` should report busy;
    each ``wait_any()`` decrements it (so a parked loop eventually drains).
    """

    def __init__(self, pending: int = 0):
        self.pending = pending
        self.wait_any_calls = 0

    def has_pending(self) -> bool:
        return self.pending > 0

    @property
    def pending_count(self) -> int:
        return self.pending

    async def wait_any(self) -> None:
        self.wait_any_calls += 1
        if self.pending > 0:
            self.pending -= 1


class FakeOutputEngine:
    run_id = "fake-output-run"

    def __init__(self, *, accepted: bool = True):
        self.accepted = accepted
        self.candidates = []
        self.commit_calls = 0

    @property
    def has_restored_terminal_output(self):
        return False

    async def evaluate(self, candidate):
        from mote.contracts.output import OutputEvaluation

        self.candidates.append(candidate)
        return OutputEvaluation(accepted=self.accepted, value=candidate.raw if self.accepted else None)

    async def commit(self):
        from mote.contracts.output import CommittedOutput

        self.commit_calls += 1
        return CommittedOutput("fake", "mote.text@1", "sha", None)


# ---------------------------------------------------------------------------
# Builders / fixtures
# ---------------------------------------------------------------------------


def make_flow_context(**overrides) -> FlowContext:
    """Build a :class:`FlowContext` with sensible test defaults."""
    from mote.contracts.schema import MessageQueue

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
    return FlowContext(**params)


@dataclass
class FlowBundle:
    """The engine plus every fake wired into it (for inspection in tests)."""

    engine: AgentFlowEngine
    ctx: FlowContext
    think_engine: FakeThinkEngine
    channel: FakeChannel
    executor: FakeExecutor
    memory: FakeMemory
    provider: FakeContextProvider
    active: list  # single-element bool holder
    bg_pool_holder: list  # single-element [FakeBgPool | None]
    reported: list  # think results published via report_think_result

    @property
    def buffer(self):
        return self.ctx.msg_buffer


@pytest.fixture
def make_engine():
    """Factory: build a fully wired :class:`AgentFlowEngine` and its fakes.

    Pass keyword overrides for any collaborator or for ``FlowContext`` fields
    (anything not a known collaborator is forwarded to ``make_flow_context``).
    """

    def _factory(
        *,
        ctx: Optional[FlowContext] = None,
        think_engine: Optional[FakeThinkEngine] = None,
        channel: Optional[FakeChannel] = None,
        executor: Optional[FakeExecutor] = None,
        memory: Optional[FakeMemory] = None,
        provider: Optional[FakeContextProvider] = None,
        active: bool = True,
        bg_pool: Optional[FakeBgPool] = None,
        turn_context_bus=None,
        get_cwd: Optional[Callable[[], str]] = None,
        durable_runner=None,
        output_engine=None,
        graph_builder=None,
        drain_writes=None,
        **ctx_overrides,
    ) -> FlowBundle:
        ctx = ctx or make_flow_context(**ctx_overrides)
        think_engine = think_engine or FakeThinkEngine()
        channel = channel or FakeChannel()
        executor = executor or FakeExecutor()
        memory = memory or FakeMemory()
        provider = provider or FakeContextProvider(ctx)
        provider.channel = channel
        output_engine = output_engine or FakeOutputEngine()

        active_holder = [active]
        bg_holder = [bg_pool]
        reported: list = []

        def is_active() -> bool:
            return active_holder[0]

        def set_active(v: bool) -> None:
            active_holder[0] = v

        def get_bg_pool():
            return bg_holder[0]

        def report_think_result(result) -> None:
            reported.append(result)

        engine_kwargs = {}
        if graph_builder is not None:
            engine_kwargs["graph_builder"] = graph_builder
        engine = AgentFlowEngine(
            think_engine=think_engine,
            command_channel=channel,
            executor=executor,
            memory=memory,
            context_provider=provider,
            is_active=is_active,
            set_active=set_active,
            get_bg_pool=get_bg_pool,
            report_think_result=report_think_result,
            turn_context_bus=turn_context_bus,
            get_cwd=get_cwd,
            durable_runner=durable_runner,
            output_engine=output_engine,
            drain_writes=drain_writes,
            **engine_kwargs,
        )
        return FlowBundle(
            engine=engine,
            ctx=ctx,
            think_engine=think_engine,
            channel=channel,
            executor=executor,
            memory=memory,
            provider=provider,
            active=active_holder,
            bg_pool_holder=bg_holder,
            reported=reported,
        )

    return _factory
