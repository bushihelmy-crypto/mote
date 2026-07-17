"""
ReActLoop — the default think→act react cycle.

The loop reads/writes the shared `active` signal via injected callables,
because `active` doubles as a
tool→loop kill switch: the End tool (and ask_user's "stop") call
Role.deactivate(), which must still be able to break this loop. Everything else
is a plain component.

The loop also owns the observe step: pull from the msg_buffer, filter by
watch/addresses, commit to the memory store (ContextManager). It is inlined here
so the loop is self-contained.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Optional

from mote.common.base import BaseLoop, LoopContext
from mote.common.base.command_channel import join_command_outputs
from mote.common.const.message import MESSAGE_ROUTE_TO_ALL
from mote.common.disk import get_disk_writer
from mote.common.events import span
from mote.common.logs import log_class
from mote.common.schema import AIMessage, CauseBy, Message, MessagePriority, UserMessage

if TYPE_CHECKING:
    from mote.common.base import BaseThinkEngine
    from mote.common.interface import BackgroundPool, MessageStore
    from mote.context.turn_context import TurnContextBus
    from mote.executor.base_executor import BaseToolExecutor
    from mote.parser import CommandChannel
    from mote.roles.context_provider import BaseContextProvider


#: Placeholder react result before any action runs; overwritten on the first
#: act, so it only ever surfaces if the loop returns without acting.
_NO_ACTIONS_YET = "No actions taken yet"


@log_class(level="DEBUG")
class ReActLoop(BaseLoop):
    """Think→act cycle with protocol-aware termination.

    Injected with reusable components only (散参 / scatter injection):
      - think_engine / command_channel / executor / memory: live collaborators
      - context_provider: packs each flow's params (see ContextProvider). The
        loop pulls its static LoopContext from it (loop_context()) at run() start,
        calls prepare() each think turn, and asks it to resolve_llm() the LLM via
        the router only when an LLM is actually needed.
      - is_active / set_active: read/write the shared `active` signal (state-backed)
      - get_bg_pool: returns the current BackgroundTaskPool or None (lazy; a tool
        may create it mid-react, so we read it fresh each turn)
      - report_think_result: publishes this turn's ThinkResult to shared state the
        moment the think task drains, so a tool running later in the same act step
        (e.g. ``end_session``) reads the fresh result off state rather than the
        think-engine machinery (which is a stateless per-turn factory).

    The loop owns the observe step: pop from msg_buffer → filter by watch/name →
    commit to the memory store.
    """

    def __init__(
        self,
        *,
        think_engine: "BaseThinkEngine",
        command_channel: "CommandChannel",
        executor: "BaseToolExecutor",
        memory: "MessageStore",
        context_provider: "BaseContextProvider",
        is_active: Callable[[], bool],
        set_active: Callable[[bool], None],
        get_bg_pool: Callable[[], Optional["BackgroundPool"]],
        report_think_result: Callable[[Any], None],
        turn_context_bus: Optional["TurnContextBus"] = None,
        get_cwd: Optional[Callable[[], str]] = None,
    ):
        self._think_engine = think_engine
        self._channel = command_channel
        self._executor = executor
        self._memory = memory
        self._context_provider = context_provider
        self._is_active = is_active
        self._set_active = set_active
        self._get_bg_pool = get_bg_pool
        self._report_think_result = report_think_result
        # The persistent (save_to_context) turn-context bucket. Recorded into
        # memory each think cycle, symmetric with the ephemeral bucket that
        # PromptBuilder appends to the user prompt — same owner (the loop), same
        # timing (right before think), so the two buckets never drift apart.
        self._turn_context_bus = turn_context_bus
        # Live cwd accessor (working_dir can move via ``cd``); ``None`` => "".
        self._get_cwd = get_cwd

        # The static observe + loop-control bundle. Filled at run() start from
        # context_provider.loop_context() — the loop never receives it directly.
        self._ctx: LoopContext | None = None

        # Recovery support: tracks the last message committed by observe.
        self.latest_observed_msg: Message | None = None

    @property
    def ctx(self) -> LoopContext:
        """The loop-control bundle, populated at the top of ``run()``.

        All observe/think/act helpers run inside ``run()`` after ``_ctx`` is
        set, so it is never None on those paths; assert to narrow the Optional
        for type checkers (and to fail loudly on any future misuse).
        """
        assert self._ctx is not None, "LoopContext accessed before run() initialized it"
        return self._ctx

    # ------------------------------------------------------------------
    # Observe — pull from buffer, filter, commit to memory store
    # ------------------------------------------------------------------

    async def _observe(self, max_priority: int = MessagePriority.NEXT) -> int:
        """Pop messages from the buffer, filter, commit to memory.

        Returns the count of new messages that passed the filter (the "news").
        """
        ctx = self.ctx
        if ctx.msg_buffer is None:
            return 0

        news_raw = ctx.msg_buffer.pop_all(max_priority=max_priority)
        if not news_raw:
            return 0

        # Dedup against already-stored history when memory is enabled.
        old_messages = [] if not ctx.enable_memory else self._memory.get()
        filtered = [
            n
            for n in news_raw
            if (n.cause_by in ctx.watch or ctx.name in n.send_to or MESSAGE_ROUTE_TO_ALL in n.send_to)
            and n not in old_messages
        ]

        # Commit to memory store.
        if ctx.observe_all:
            await self._memory.add_batch(news_raw)
        else:
            await self._memory.add_batch(filtered)

        self.latest_observed_msg = filtered[-1] if filtered else None

        return len(filtered)

    # ------------------------------------------------------------------
    # Single-step primitives (were Role._think / _act / _finish_react)
    # ------------------------------------------------------------------

    async def _record_turn_context(self) -> None:
        """Commit this cycle's persistent (save_to_context) turn-context block.

        Renders the bus's persisted bucket (git status / token pressure / LSP
        diagnostics / ... — everything not flagged ``save_to_context=False``) and,
        when non-empty, appends it to history as a user message *before* the
        request is assembled, so the block both survives into future turns and is
        visible to this cycle's think. Committing it here — right before think,
        after ``_observe`` has already committed the turn's user prompt — is what
        keeps history (and the durable rollout) in ``prompt → turn-context``
        order. Symmetric with the ephemeral bucket, which PromptBuilder collects
        into the user prompt on the same cycle. Best-effort: change-gated sources
        self-suppress, so a quiet cycle adds nothing.
        """
        bus = self._turn_context_bus
        if bus is None:
            return
        cwd = self._get_cwd() if self._get_cwd is not None else None
        block = await bus.collect_to_context(cwd=cwd or None)
        if block:
            await self._memory.add(UserMessage(content=block))

    async def _step_think(self) -> bool:
        """Use LLM to decide whether and what to do next.

        Returns False immediately when the shared `active` signal is off (e.g.
        the End tool called deactivate during the previous act), terminating XML.
        """
        if not self._is_active():
            return False

        # Persist this cycle's turn-context block before building the request, so
        # it lands in history after the user prompt (correct order) and is seen by
        # this think. Mirrors the ephemeral bucket collected inside prepare().
        await self._record_turn_context()

        async with span("think"):
            tr = await self._context_provider.prepare()
            # Trigger the router only now that an LLM is actually needed, picking the
            # model from this request's messages when intelligent routing is enabled.
            llm = await self._context_provider.resolve_llm(tr.req)
            await self._think_engine.start(tr.req, tr.system_prompt, tool_specs=tr.tool_specs, llm=llm)
        return True

    async def _step_act(self) -> Message:
        async with span("act"):
            valid_names = set(self.ctx.tools)
            commands = [cmd async for cmd in self._channel.iter_commands(self._think_engine, valid_names)]

            # The think task has now drained (iter_commands joined it), so the
            # result is final. Publish it to shared state *before* running any
            # command, so a tool in the loop below (e.g. ``end_session``) reads
            # this turn's fresh result off state rather than the engine.
            self._report_think_result(self._think_engine.result)
            content = self._think_engine.result.content

            # Build the per-command entries up front (id/name/args are known from
            # the parsed calls; output/success fill in as each body runs). Having
            # the full skeleton before execution lets the channel record the
            # assistant tool-call message ahead of any side effect.
            executed: list[dict] = [
                {
                    "id": cmd.get("id"),
                    "name": cmd["command_name"],
                    "args": cmd.get("args") or {},
                    "output": "",
                    "success": True,
                }
                for cmd in commands
            ]

            # Durability checkpoint: when this turn calls an EXTERNAL-effect tool
            # that the executor will ledger, persist the assistant's tool-call
            # message and FLUSH it to disk *before* running any body. A crash
            # mid-side-effect then leaves a dangling tool_call on resume that the
            # ledger can heal (pair with the recorded/unknown result), instead of
            # losing the whole turn (and the call id needed to reconcile it).
            # Non-EXTERNAL turns skip this and keep the cheaper single-shot
            # record_turn below — no early append, no drain. Layering: the loop
            # (not the executor) owns drain timing; it only asks the durable
            # writer to flush what the recorder subscriber already queued.
            checkpoint = any(self._executor.will_ledger(e["name"], e["id"]) for e in executed)
            if checkpoint:
                await self._channel.record_call(self._memory, content, executed)
                await get_disk_writer().drain()

            # Execute in order. On the first failure, stop running further commands
            # but still RECORD a result for each remaining one: native tool-use
            # requires every emitted tool_call to have a paired tool_result, so we
            # cannot simply drop them.
            failed = False
            try:
                for entry in executed:
                    name = entry["name"]
                    if failed:
                        entry["output"] = (
                            f"[SKIPPED] Command {name} was not executed because an earlier "
                            "command failed. Please replan in the next round."
                        )
                        entry["success"] = False
                        entry["settled"] = True
                        continue
                    result = await self._executor.run_command(name, entry["args"], result_id=entry["id"])
                    entry["output"] = result.output
                    entry["success"] = result.success
                    # Media (base64 images / PDFs) surfaced to the model as a supplemental
                    # multimodal message by the channel's record_results.
                    if result.images:
                        entry["images"] = result.images
                    if result.pdfs:
                        entry["pdfs"] = result.pdfs
                    # Per-result lifecycle hint (erasable/pin). Carried like media so
                    # the channel can stamp it onto the tool_result message metadata;
                    # only the native channel (which has per-result messages) uses it.
                    if result.retention:
                        entry["retention"] = result.retention
                    # Resource provenance of a reconstructable result (the file a Read
                    # derived from). Carried the same way; the channel stamps it onto
                    # the tool_result metadata for ContextVisibility to key off.
                    if result.resource_path:
                        entry["resource_path"] = result.resource_path
                    # Structured payload (only SearchTools' {tool_references} is read by
                    # the recorder → ToolMessage.tool_references for the Anthropic
                    # server-side tool-search wire projection). Other tools' data is
                    # ignored downstream.
                    if result.data is not None:
                        entry["data"] = result.data
                    entry["settled"] = True
                    if not result.success:
                        failed = True
                    # A terminal block (user rejected the approval prompt, or a hook
                    # vetoed the call) ends the whole react loop, not just this call.
                    # Clear the active signal — the same kill switch the End tool trips
                    # — so the next _step_think returns False and the loop stops. Later
                    # commands are still recorded as [SKIPPED] via ``failed`` above, so
                    # native tool-use keeps every tool_call paired with a tool_result.
                    if result.terminate:
                        self._set_active(False)
            except BaseException:
                # Interrupted mid-execution — the common case is a Ctrl+C, which
                # AgentControl.interrupt turns into a task ``cancel()`` →
                # ``CancelledError`` (a BaseException) raised at the ``await`` inside
                # run_command, before the loop finished. In the CHECKPOINT path the
                # assistant tool_call message is ALREADY in history (record_call ran
                # up front to make a mid-side-effect crash healable). Unwinding now
                # without a paired tool_result for every id would leave a dangling
                # tool_use → the NEXT provider request violates the "each tool_use
                # needs an immediately following tool_result" rule (Anthropic /
                # Bedrock 400). So close the pairing here, at the point of truth
                # (we KNOW it was interrupted — more precise than the resume-time
                # ledger reconcile, which can only guess ``unknown-after-crash``):
                # synthesize an ``[INTERRUPTED]`` result for every call that had not
                # settled, record the results, then re-raise so the normal
                # interrupt/recovery unwind still runs. The non-checkpoint path has
                # recorded NOTHING yet (record_turn runs only on the success tail
                # below), so it has no dangling call to repair — hence the guard.
                if checkpoint:
                    for entry in executed:
                        if not entry.get("settled"):
                            entry["output"] = "[INTERRUPTED] Command did not complete (the turn was interrupted)."
                            entry["success"] = False
                    await self._channel.record_results(self._memory, executed)
                raise

            outputs = join_command_outputs(executed)

            # Write this turn into memory in the channel's protocol shape (XML:
            # text + merged outputs; native: tool_calls + per-call tool results).
            # If a checkpoint already recorded the assistant message, only the
            # results remain; otherwise record the whole round in one shot.
            if checkpoint:
                await self._channel.record_results(self._memory, executed)
            else:
                await self._channel.record_turn(self._memory, content, executed)

            await self._think_engine.join()

            # The published react result is protocol-flavored: XML asks the
            # orchestrator to mark the task finished, native returns the plain
            # outputs. The channel owns that phrasing (see react_result).
            return AIMessage(
                content=self._channel.react_result(outputs),
                sent_from=self.ctx.name,
                cause_by=CauseBy.RUN_COMMAND,
            )

    async def _finish(self) -> Message:
        """Finalize a native turn that ended without tool calls.

        The model signalled completion by replying with plain text and no
        tool_calls. Record that text as the assistant's final turn (so memory
        stays consistent) and return it as the react response. No commands ran,
        so there is nothing to execute and the loop stops.
        """
        content = self._think_engine.result.content or ""
        self._report_think_result(self._think_engine.result)
        await self._channel.record_turn(self._memory, content, [])
        await self._think_engine.join()
        return AIMessage(
            content=content,
            sent_from=self.ctx.name,
            cause_by=CauseBy.RUN_COMMAND,
        )

    async def run(self) -> Message | None:
        # Pull the static observe + loop-control bundle once per run(). The loop
        # holds only the provider; it never receives LoopContext directly.
        self._ctx = self._context_provider.loop_context()

        # Initial gate: if no messages observed, nothing to do.
        if not await self._observe():
            return None

        self._set_active(True)

        rsp = AIMessage(content=_NO_ACTIONS_YET, cause_by=CauseBy.ACTION)
        while True:
            if await self._observe(max_priority=MessagePriority.NEXT):
                self._set_active(True)
            # Budget gate — the run's spend ceiling. Checked before think so a
            # hard cap halts the loop *before* any LLM access; the provider owns
            # the spend read + threshold events (soft notice at 80%, hard stop at
            # 100%). An unbudgeted agent returns PROCEED, and termination then
            # rests entirely on the natural exits below (no-todo / terminal).
            verdict = await self._context_provider.enforce_budget()
            if verdict.stop:
                rsp = AIMessage(content=verdict.message, sent_from=self.ctx.name, cause_by=CauseBy.RUN_COMMAND)
                break
            # think
            has_todo = await self._step_think()
            if not has_todo:
                bg_pool = self._get_bg_pool()
                if bg_pool and bg_pool.has_pending():
                    # Block until one background task settles, then re-observe so
                    # its result message can drive another think round.
                    await bg_pool.wait_any()
                    await self._observe(max_priority=MessagePriority.NEXT)
                    self._set_active(True)
                    continue
                break
            # Protocol-aware termination. XML ends when the model emits an `End`
            # command (which deactivates the Role, so the next think returns
            # False above); native ends when the model stops calling tools and
            # returns a plain text reply. On a terminal native turn there are no
            # commands to run, so skip act — instead capture the final text as
            # the response and stop.
            if await self._channel.is_terminal(self._think_engine):
                rsp = await self._finish()
                break
            # act
            rsp = await self._step_act()

        return rsp
