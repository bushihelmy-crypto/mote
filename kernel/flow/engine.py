"""
AgentFlowEngine — the graph-driven agent flow runtime.

The engine reads/writes the shared `active` signal via injected callables,
because `active` doubles as a
tool→flow kill switch: the End tool (and ask_user's "stop") call
Role.deactivate(), which must still be able to stop the active flow. Everything else
is a plain component.

The flow also owns the observe step: pull from the msg_buffer, filter by
watch/addresses, commit to the memory store (ContextManager). It is inlined here
so the execution kernel is self-contained.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable
from typing import TYPE_CHECKING, Any, Callable, Optional
from uuid import uuid4

from mote.contracts.schema import AIMessage, CauseBy, Message
from mote.kernel.flow.context import FlowContext
from mote.kernel.flow.events import (
    RunCancelled,
    RunEvent,
    RunFailed,
    RunPhase,
    RunPhaseCompleted,
    RunPhaseStarted,
    RunStarted,
    RunSucceeded,
)
from mote.kernel.flow.graph.core import AgentGraph
from mote.kernel.flow.graph.react import build_react_graph
from mote.kernel.flow.recovery import DurableFlowRunner, NodeAttempt
from mote.kernel.flow.result import FlowResult
from mote.kernel.flow.services import (
    ActionDispatcher,
    ActionExecutionService,
    FlowOutputService,
    FlowServices,
    ObservationService,
    TextCompletionPolicy,
    ThinkService,
)
from mote.kernel.flow.slo import DEFAULT_RUNTIME_SLO
from mote.kernel.flow.state import FlowState
from mote.kernel.flow.think_checkpoint import ThinkCheckpoint

if TYPE_CHECKING:
    from mote.contracts.background_tasks import BackgroundTaskService
    from mote.contracts.ports import MessageStore
    from mote.kernel.parser import CommandChannel
    from mote.kernel.think.base import BaseThinkEngine


#: Placeholder react result before any action runs; overwritten on the first
#: act, so it only ever surfaces if the flow returns without acting.
_NO_ACTIONS_YET = "No actions taken yet"
RUN_EVENT_BUFFER_SIZE = DEFAULT_RUNTIME_SLO.run_event_buffer


async def _noop_async() -> None:
    return None


_PUBLIC_PHASE = {
    "restore": RunPhase.RECOVERY,
    "observe": RunPhase.OBSERVATION,
    "budget": RunPhase.BUDGET,
    "think": RunPhase.MODEL,
    "interpret": RunPhase.INTERPRETATION,
    "act": RunPhase.ACTION,
    "validate_output": RunPhase.OUTPUT,
    "wait_background": RunPhase.WAIT,
}


class AgentFlowEngine:
    """Think→act cycle with protocol-aware termination.

    Injected with reusable components only (散参 / scatter injection):
      - think_engine / command_channel / executor / memory: live collaborators
      - context_provider: packs each flow's params (see ContextProvider). The
        flow pulls its static FlowContext from it at run() start,
        calls prepare() each think turn, and asks it to resolve_llm() the LLM via
        the router only when an LLM is actually needed.
      - is_active / set_active: read/write the shared `active` signal (state-backed)
      - get_bg_pool: returns the current BackgroundTaskPool or None (lazy; a tool
        may create it during a flow, so we read it fresh each turn)
      - report_think_result: publishes this turn's ThinkResult to shared state the
        moment the think task drains, so a tool running later in the same act step
        (e.g. ``end_session``) reads the fresh result off state rather than the
        think-engine machinery (which is a stateless per-turn factory).

    The flow owns the observe step: pop from msg_buffer → filter by watch/name →
    commit to the memory store.
    """

    def __init__(
        self,
        *,
        think_engine: "BaseThinkEngine",
        command_channel: "CommandChannel",
        executor: Any,
        memory: "MessageStore",
        context_provider: Any,
        is_active: Callable[[], bool],
        set_active: Callable[[bool], None],
        get_bg_pool: Callable[[], Optional["BackgroundTaskService"]],
        report_think_result: Callable[[Any], None],
        turn_context_bus: Any = None,
        get_cwd: Optional[Callable[[], str]] = None,
        advance_turn: Optional[Callable[[], int]] = None,
        durable_runner: Any = None,
        completion_policy=None,
        action_dispatcher=None,
        output_engine=None,
        drain_writes: Callable[[], Awaitable[None]] | None = None,
        graph_builder: Callable[
            [FlowServices],
            AgentGraph[FlowState[Any], FlowResult[Any] | None],
        ] = build_react_graph,
    ):
        self._think_engine = think_engine
        self._channel = command_channel
        self._turn_channel = command_channel
        self._executor = executor
        self._memory = memory
        self._context_provider = context_provider
        self._is_active = is_active
        self._set_active = set_active
        self._get_bg_pool = get_bg_pool
        self._report_think_result = report_think_result
        # Durable think seam (A3, G1). When present, each think round is
        # memoized in the run journal so a resume can reinstate a completed
        # result instead of re-paying the model. ``None`` (durable disabled)
        # makes every durable hook below a no-op — byte-for-byte the old path.
        self._durable_runner = durable_runner
        # The persistent (save_to_context) turn-context bucket. Recorded into
        # memory each think cycle, symmetric with the ephemeral bucket that
        # PromptBuilder appends to the user prompt — same owner (the flow), same
        # timing (right before think), so the two buckets never drift apart.
        self._turn_context_bus = turn_context_bus
        # Live cwd accessor (working_dir can move via ``cd``); ``None`` => "".
        self._get_cwd = get_cwd
        # Advances the turn (prompt) index once per think round so change hunks
        # captured during a turn are attributed to it; ``None`` => no-op (the
        # counter simply never moves, harmless when hunk tracking is unused).
        self._advance_turn = advance_turn
        self._completion_policy = completion_policy or TextCompletionPolicy()
        self._action_dispatcher = action_dispatcher or ActionDispatcher()
        if output_engine is None:
            raise TypeError("output_engine is required")
        self._output_engine = output_engine
        self._drain_writes = drain_writes or _noop_async
        self._event_queues: set[asyncio.Queue[RunEvent]] = set()
        self._current_run_id = ""

        # The static observe + flow-control bundle. Filled at run() start from
        # context_provider.flow_context() — the engine never receives it directly.
        self._ctx: FlowContext | None = None

        self._think_checkpoint = ThinkCheckpoint(
            journal_runner=self._durable_runner,
            memory=self._memory,
            think_engine=self._think_engine,
        )
        self._think = ThinkService(
            is_active=self._is_active,
            checkpoint=self._think_checkpoint,
            context_provider=self._context_provider,
            think_engine=self._think_engine,
            output_engine=self._output_engine,
            memory=self._memory,
            set_channel=self._set_turn_channel,
            turn_context_bus=self._turn_context_bus,
            get_cwd=self._get_cwd,
        )
        self._observation = ObservationService(context=lambda: self.ctx, memory=self._memory)
        self._actions = ActionExecutionService(
            context=lambda: self.ctx,
            channel=lambda: self._turn_channel,
            think_engine=self._think_engine,
            executor=self._executor,
            memory=self._memory,
            report_think_result=self._report_think_result,
            complete_think=self._think_checkpoint.complete,
            reap_think=self._think_checkpoint.reap,
            set_active=self._set_active,
            drain_writes=self._drain_writes,
            dispatcher=self._action_dispatcher,
        )
        self._outputs = FlowOutputService(
            context=lambda: self.ctx,
            channel=lambda: self._turn_channel,
            think_engine=self._think_engine,
            memory=self._memory,
            output_engine=self._output_engine,
            report_think_result=self._report_think_result,
            complete_think=self._think_checkpoint.complete,
            reap_think=self._think_checkpoint.reap,
            drain_writes=self._drain_writes,
        )
        self._services = FlowServices(
            context=lambda: self.ctx,
            observation=self._observation,
            think=self._think,
            actions=self._actions,
            outputs=self._outputs,
            context_provider=self._context_provider,
            completion_policy=self._completion_policy,
            current_channel=lambda: self._turn_channel,
            think_engine=self._think_engine,
            set_active=self._set_active,
            get_bg_pool=self._get_bg_pool,
            advance_turn=self._advance_turn,
        )
        self._graph = graph_builder(self._services)
        self._flow_runner = DurableFlowRunner(
            self._graph,
            on_cancel=self._think_checkpoint.reap,
            on_failure=self._think_checkpoint.fail,
            on_node_started=self._node_started,
            on_node_completed=self._node_completed,
        )

    @property
    def ctx(self) -> FlowContext:
        """The flow-control bundle, populated at the top of ``run()``.

        All observe/think/act helpers run inside ``run()`` after ``_ctx`` is
        set, so it is never None on those paths; assert to narrow the Optional
        for type checkers (and to fail loudly on any future misuse).
        """
        assert self._ctx is not None, "FlowContext accessed before run() initialized it"
        return self._ctx

    @property
    def latest_observed_msg(self) -> Message | None:
        return self._observation.latest_observed_message

    def _set_turn_channel(self, channel) -> None:
        self._turn_channel = channel

    async def _emit_run_event(self, event: RunEvent) -> None:
        for queue in tuple(self._event_queues):
            await queue.put(event)

    async def _node_started(self, attempt: NodeAttempt) -> None:
        if self._current_run_id:
            await self._emit_run_event(RunPhaseStarted(self._current_run_id, _PUBLIC_PHASE[attempt.node_id.value]))

    async def _node_completed(self, attempt: NodeAttempt) -> None:
        if self._current_run_id:
            await self._emit_run_event(RunPhaseCompleted(self._current_run_id, _PUBLIC_PHASE[attempt.node_id.value]))

    # ------------------------------------------------------------------
    # Observe — pull from buffer, filter, commit to memory store
    # ------------------------------------------------------------------

    async def run(self) -> FlowResult[Any] | None:
        """Execute the configured validated graph to a terminal transition."""
        run_id = uuid4().hex
        self._current_run_id = run_id
        await self._emit_run_event(RunStarted(run_id))
        self._ctx = self._context_provider.flow_context()
        state = FlowState[Any](response=AIMessage(content=_NO_ACTIONS_YET, cause_by=CauseBy.ACTION))
        try:
            result = await self._flow_runner.run(state)
        except asyncio.CancelledError:
            await self._emit_run_event(RunCancelled(run_id))
            raise
        except Exception as exc:
            await self._emit_run_event(RunFailed(run_id, type(exc).__name__, str(exc)))
            raise
        else:
            await self._emit_run_event(RunSucceeded(run_id, result))
            return result
        finally:
            self._current_run_id = ""

    async def run_events(self) -> AsyncIterator[RunEvent]:
        """Execute once and yield stable semantic events until a terminal event."""
        queue: asyncio.Queue[RunEvent] = asyncio.Queue(maxsize=RUN_EVENT_BUFFER_SIZE)
        self._event_queues.add(queue)
        task = asyncio.create_task(self.run(), name="mote-flow-run-events")
        terminal = (RunSucceeded, RunFailed, RunCancelled)
        try:
            while True:
                event = await queue.get()
                yield event
                if isinstance(event, terminal):
                    break
            await asyncio.gather(task, return_exceptions=True)
        finally:
            self._event_queues.discard(queue)
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
