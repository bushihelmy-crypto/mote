"""
ExecutionEngine — the graph-driven agent flow runtime.

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
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Callable, Generic, Optional, TypeVar
from uuid import uuid4

from mote.contracts.conversation import AIMessage, CauseBy, Message
from mote.contracts.execution.restore import ExecutionRestorePort
from mote.contracts.model.inference import InferenceResult
from mote.contracts.ports.conversation.message_activity import MessageActivity
from mote.contracts.ports.conversation.turn_context_bus import TurnContextCollector
from mote.contracts.ports.execution.model_turn_completion import ModelTurnCompletionPolicy
from mote.contracts.ports.execution.pending_act import PendingActAcceptancePort
from mote.contracts.ports.execution.transaction import ExecutionOutputTransactionPort
from mote.contracts.ports.output.evaluation import OutputEngine
from mote.contracts.ports.task.operations import BackgroundTaskService
from mote.contracts.tool.catalog import ToolBindingSnapshot, ToolExecutionOutcome, ToolExecutionPort
from mote.kernel.execution.context import ExecutionContext
from mote.kernel.execution.context_provider import BaseContextProvider
from mote.kernel.execution.events import (
    RunCancelled,
    RunCompletionSummary,
    RunEvent,
    RunFailed,
    RunPhase,
    RunPhaseCompleted,
    RunPhaseStarted,
    RunStarted,
    RunSucceeded,
)
from mote.kernel.execution.graph.core import AgentGraph
from mote.kernel.execution.graph.react import build_react_graph
from mote.kernel.execution.limits import DEFAULT_EXECUTION_LIMITS
from mote.kernel.execution.operations import (
    ActionDispatcher,
    ActionExecutionService,
    GraphAssemblyInputs,
    InferenceService,
    ObservationService,
    OutputOperation,
    TextCompletionPolicy,
)
from mote.kernel.execution.recovery import EffectAwareGraphRunner, NodeAttempt
from mote.kernel.execution.result import ExecutionResult
from mote.kernel.execution.state import ExecutionState

if TYPE_CHECKING:
    from mote.contracts.ports.conversation.message_store import MessageStore
    from mote.contracts.ports.execution.checkpoint import InferenceCheckpointPort
    from mote.contracts.ports.task.operations import BackgroundTaskService
    from mote.kernel.commands import CommandChannel
    from mote.kernel.inference.base import BaseInferenceEngine


#: Placeholder react result before any action runs; overwritten on the first
#: act, so it only ever surfaces if the flow returns without acting.
_NO_ACTIONS_YET = "No actions taken yet"
RUN_EVENT_BUFFER_SIZE = DEFAULT_EXECUTION_LIMITS.run_event_buffer
OutputT = TypeVar("OutputT")


_PUBLIC_PHASE = {
    "restore": RunPhase.RECOVERY,
    "observe": RunPhase.OBSERVATION,
    "budget": RunPhase.BUDGET,
    "think": RunPhase.MODEL,
    "interpret": RunPhase.INTERPRETATION,
    "act": RunPhase.ACTION,
    "validate_output": RunPhase.OUTPUT,
    "await_quiescence": RunPhase.WAIT,
}


class ExecutionEngine(Generic[OutputT]):
    """Think→act cycle with protocol-aware termination.

    Injected with reusable components only (散参 / scatter injection):
      - inference_engine / command_channel / executor / memory: live collaborators
      - context_provider: packs each flow's params (see ContextProvider). The
        flow pulls its static ExecutionContext from it at run() start,
        calls prepare() each think turn, and asks it to resolve_llm() the LLM via
        the router only when an LLM is actually needed.
      - is_active / set_active: read/write the shared `active` signal (state-backed)
      - get_bg_pool: returns the current BackgroundTaskPool or None (lazy; a tool
        may create it during a flow, so we read it fresh each turn)
      - report_inference_result: publishes this turn's InferenceResult to shared state the
        moment the think task drains, so a tool running later in the same act step
        (e.g. ``end_session``) reads the fresh result off state rather than the
        think-engine machinery (which is a stateless per-turn factory).

    The flow owns the observe step: pop from msg_buffer → filter by watch/name →
    commit to the memory store.
    """

    def __init__(
        self,
        *,
        inference_engine: "BaseInferenceEngine",
        command_channel: "CommandChannel",
        executor: ToolExecutionPort[ToolExecutionOutcome],
        tool_execution_port: ToolExecutionPort[ToolExecutionOutcome],
        memory: "MessageStore",
        context_provider: BaseContextProvider,
        is_active: Callable[[], bool],
        set_active: Callable[[bool], None],
        get_bg_pool: Callable[[], Optional["BackgroundTaskService"]],
        report_inference_result: Callable[[InferenceResult], None],
        inference_checkpoint: "InferenceCheckpointPort",
        execution_transaction: ExecutionOutputTransactionPort[OutputT],
        pending_act_acceptance: PendingActAcceptancePort,
        execution_restore: ExecutionRestorePort[OutputT],
        turn_context_bus: TurnContextCollector | None = None,
        get_cwd: Optional[Callable[[], str]] = None,
        advance_turn: Optional[Callable[[], int]] = None,
        completion_policy: ModelTurnCompletionPolicy | None = None,
        action_dispatcher: ActionDispatcher | None = None,
        output_engine: OutputEngine[OutputT] | None = None,
        graph_builder: Callable[
            [GraphAssemblyInputs[OutputT]],
            AgentGraph[ExecutionState[OutputT], ExecutionResult[OutputT] | None],
        ] = build_react_graph,
    ):
        self._inference_engine = inference_engine
        self._channel = command_channel
        self._turn_channel = command_channel
        self._executor = executor
        self._tool_execution_port = tool_execution_port
        self._tool_snapshot = None
        self._memory = memory
        self._context_provider = context_provider
        self._is_active = is_active
        self._set_active = set_active
        self._get_bg_pool = get_bg_pool
        self._report_think_result = report_inference_result
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
        self._event_queues: set[asyncio.Queue[RunEvent]] = set()
        self._current_run_id = ""

        # The static observe + flow-control bundle. Filled at run() start from
        # context_provider.execution_context() — the engine never receives it directly.
        self._ctx: ExecutionContext | None = None

        self._inference_checkpoint = inference_checkpoint
        self._inference = InferenceService(
            is_active=self._is_active,
            checkpoint=self._inference_checkpoint,
            context_provider=self._context_provider,
            inference_engine=self._inference_engine,
            output_engine=self._output_engine,
            transaction=execution_transaction,
            set_channel=self._set_turn_channel,
            set_tool_snapshot=self._set_tool_snapshot,
            turn_context_bus=self._turn_context_bus,
            get_cwd=self._get_cwd,
        )
        self._observation = ObservationService(
            context=lambda: self.ctx,
            history_reader=self._memory,
            transaction=execution_transaction,
        )
        self._actions = ActionExecutionService(
            context=lambda: self.ctx,
            channel=lambda: self._turn_channel,
            inference_engine=self._inference_engine,
            tool_execution_port=self._tool_execution_port,
            tool_snapshot=lambda: self._tool_snapshot,
            transaction=execution_transaction,
            pending_act_acceptance=pending_act_acceptance,
            report_inference_result=self._report_think_result,
            set_active=self._set_active,
            dispatcher=self._action_dispatcher,
        )
        self._outputs = OutputOperation(
            context=lambda: self.ctx,
            channel=lambda: self._turn_channel,
            inference_engine=self._inference_engine,
            transaction=execution_transaction,
            output_engine=self._output_engine,
            report_inference_result=self._report_think_result,
        )
        self._graph_inputs = GraphAssemblyInputs(
            context=lambda: self.ctx,
            observation=self._observation,
            inference=self._inference,
            actions=self._actions,
            outputs=self._outputs,
            restore=execution_restore,
            context_provider=self._context_provider,
            completion_policy=self._completion_policy,
            current_channel=lambda: self._turn_channel,
            inference_engine=self._inference_engine,
            set_active=self._set_active,
            inbox_activity=self._inbox_activity,
            get_bg_pool=self._get_bg_pool,
            advance_turn=self._advance_turn,
        )
        self._graph = graph_builder(self._graph_inputs)
        self._flow_runner = EffectAwareGraphRunner(
            self._graph,
            on_cancel=self._inference_checkpoint.discard,
            on_failure=self._inference_checkpoint.abort,
            on_node_started=self._node_started,
            on_node_completed=self._node_completed,
        )

    def _inbox_activity(self) -> MessageActivity:
        buffer = self.ctx.msg_buffer
        if buffer is None:
            raise RuntimeError("execution message buffer is unavailable")
        return buffer

    @property
    def ctx(self) -> ExecutionContext:
        """The flow-control bundle, populated at the top of ``run()``.

        All observe/think/act helpers run inside ``run()`` after ``_ctx`` is
        set, so it is never None on those paths; assert to narrow the Optional
        for type checkers (and to fail loudly on any future misuse).
        """
        assert self._ctx is not None, "ExecutionContext accessed before run() initialized it"
        return self._ctx

    @property
    def latest_observed_msg(self) -> Message | None:
        return self._observation.latest_observed_message

    def _set_turn_channel(self, channel: "CommandChannel") -> None:
        self._turn_channel = channel

    def _set_tool_snapshot(self, snapshot: ToolBindingSnapshot | None) -> None:
        if self._tool_snapshot is not None:
            self._tool_execution_port.release(self._tool_snapshot)
        self._tool_snapshot = snapshot

    def restore_tool_snapshot(self, snapshot: ToolBindingSnapshot) -> None:
        """Install the exact live snapshot used to validate a recovered ACT."""

        self._set_tool_snapshot(snapshot)

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

    async def run(self) -> ExecutionResult[OutputT] | None:
        """Execute the configured validated graph to a terminal transition."""
        run_id = uuid4().hex
        self._current_run_id = run_id
        await self._emit_run_event(RunStarted(run_id))
        self._ctx = self._context_provider.execution_context()
        state = ExecutionState[OutputT](response=AIMessage(content=_NO_ACTIONS_YET, cause_by=CauseBy.ACTION))
        try:
            result = await self._flow_runner.run(state)
        except asyncio.CancelledError:
            await self._emit_run_event(RunCancelled(run_id))
            raise
        except Exception as exc:
            await self._emit_run_event(RunFailed(run_id, type(exc).__name__, str(exc)))
            raise
        else:
            committed = result.committed_output if result is not None else None
            await self._emit_run_event(
                RunSucceeded(
                    run_id,
                    RunCompletionSummary(
                        committed=committed is not None,
                        candidate_id=(committed.candidate_id if committed is not None else None),
                        contract_id=(committed.contract_id if committed is not None else None),
                        presentation_kind=(type(result.presentation).__name__ if result is not None else None),
                    ),
                )
            )
            return result
        finally:
            if self._tool_snapshot is not None:
                self._tool_execution_port.release(self._tool_snapshot)
                self._tool_snapshot = None
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
