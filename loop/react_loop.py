"""
ReActLoop — the default think→act react cycle.

Ported verbatim from Role._react / _think / _act / _finish_react. The loop owns
its own iteration state (consecutive count) and reads/writes the shared `active`
signal via injected callables, because `active` doubles as a tool→loop kill
switch: the End tool (and ask_human's "stop") call Role.deactivate(), which must
still be able to break this loop. Everything else is a plain component.

The loop also owns the observe step: pull from the msg_buffer, filter by
watch/addresses, commit to the memory store (ContextManager). This was
previously in Perception; now it's inlined here so the loop is self-contained.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Optional

from metagpt.common.base import BaseLoop, LoopContext
from metagpt.common.base.command_channel import join_command_outputs
from metagpt.common.const.message import MESSAGE_ROUTE_TO_ALL
from metagpt.common.events import span
from metagpt.common.logs import log_class
from metagpt.common.prompt.output import SUMMARIZE_STATUS_WHEN_CONSECUTIVE
from metagpt.common.schema import (
    AIMessage,
    CauseBy,
    MessagePriority,
    Message,
    UserMessage,
)

if TYPE_CHECKING:
    from metagpt.common.interface import BackgroundPool, MessageStore
    from metagpt.executor.base_executor import BaseToolExecutor
    from metagpt.common.base import BaseThinkEngine
    from metagpt.parser import CommandChannel
    from metagpt.roles.context_provider import BaseContextProvider


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
    ):
        self._think_engine = think_engine
        self._channel = command_channel
        self._executor = executor
        self._memory = memory
        self._context_provider = context_provider
        self._is_active = is_active
        self._set_active = set_active
        self._get_bg_pool = get_bg_pool

        # The static observe + loop-control bundle. Filled at run() start from
        # context_provider.loop_context() — the loop never receives it directly.
        self._ctx: LoopContext | None = None

        # Loop-owned iteration state (was state.consecutive_react_cnt).
        self._consecutive = 0
        # Recovery support: tracks the last message committed by observe.
        self.latest_observed_msg: Message | None = None

    # ------------------------------------------------------------------
    # Observe — pull from buffer, filter, commit to memory store
    # ------------------------------------------------------------------

    async def _observe(self, max_priority: int = MessagePriority.NEXT) -> int:
        """Pop messages from the buffer, filter, commit to memory.

        Returns the count of new messages that passed the filter (the "news").
        """
        ctx = self._ctx
        if ctx.msg_buffer is None:
            return 0

        news_raw = ctx.msg_buffer.pop_all(max_priority=max_priority)
        if not news_raw:
            return 0

        # Dedup against already-stored history when memory is enabled.
        old_messages = [] if not ctx.enable_memory else self._memory.get()
        filtered = [
            n for n in news_raw
            if (
                n.cause_by in ctx.watch
                or ctx.name in n.send_to
                or MESSAGE_ROUTE_TO_ALL in n.send_to
            )
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

    async def _step_think(self) -> bool:
        """Use LLM to decide whether and what to do next.

        Returns False immediately when the shared `active` signal is off (e.g.
        the End tool called deactivate during the previous act), terminating XML.
        """
        if not self._is_active():
            return False

        async with span("think"):
            tr = await self._context_provider.prepare()
            # Trigger the router only now that an LLM is actually needed, picking the
            # model from this request's messages when intelligent routing is enabled.
            llm = await self._context_provider.resolve_llm(tr.req)
            await self._think_engine.start(
                tr.req, tr.system_prompt, tool_specs=tr.tool_specs, llm=llm
            )
        return True

    async def _step_act(self) -> Message:
        async with span("act"):
            valid_names = set(self._ctx.tools)
            commands = [
                cmd async for cmd in self._channel.iter_commands(self._think_engine, valid_names)
            ]

            # Execute in order. On the first failure, stop running further commands
            # but still RECORD a result for each remaining one: native tool-use
            # requires every emitted tool_call to have a paired tool_result, so we
            # cannot simply drop them.
            executed: list[dict] = []
            failed = False
            for cmd in commands:
                name = cmd["command_name"]
                entry = {"id": cmd.get("id"), "name": name, "args": cmd.get("args") or {}, "output": "", "success": True}
                if failed:
                    entry["output"] = (
                        f"[SKIPPED] Command {name} was not executed because an earlier "
                        "command failed. Please replan in the next round."
                    )
                    entry["success"] = False
                    executed.append(entry)
                    continue
                result = await self._executor.run_command(name, cmd.get("args") or {}, result_id=cmd.get("id"))
                entry["output"] = result.output
                entry["success"] = result.success
                # Media (base64 images / PDFs) surfaced to the model as a supplemental
                # multimodal message by the channel's record_turn.
                if result.images:
                    entry["images"] = result.images
                if result.pdfs:
                    entry["pdfs"] = result.pdfs
                executed.append(entry)
                if not result.success:
                    failed = True

            outputs = join_command_outputs(executed)

            # The channel writes this turn into memory in its protocol's shape
            # (XML: text + merged outputs; native: tool_calls + per-call tool results).
            await self._channel.record_turn(self._memory, self._think_engine.result.content, executed)

            await self._think_engine.join()

            # The published react result is protocol-flavored: XML asks the
            # orchestrator to mark the task finished, native returns the plain
            # outputs. The channel owns that phrasing (see react_result).
            return AIMessage(
                content=self._channel.react_result(outputs),
                sent_from=self._ctx.name,
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
        await self._channel.record_turn(self._memory, content, [])
        await self._think_engine.join()
        return AIMessage(
            content=content,
            sent_from=self._ctx.name,
            cause_by=CauseBy.RUN_COMMAND,
        )

    async def _ask_user(self, question: str, header: str, options: list[tuple[str, str]]) -> str:
        """Run the AskUserQuestion tool with a single question; return its answer.

        Consolidates the two post-check prompts in run() (the max-rounds gate and
        the consecutive-actions gate), which differ only in their question text and
        their option pairs. ``options`` is a list of ``(label, description)``.
        """
        result = await self._executor.run_command(
            "AskUserQuestion",
            {
                "questions": [
                    {
                        "question": question,
                        "header": header,
                        "options": [{"label": label, "description": desc} for label, desc in options],
                    }
                ]
            },
        )
        return result.output

    async def run(self) -> Message | None:
        # Pull the static observe + loop-control bundle once per run(). The loop
        # holds only the provider; it never receives LoopContext directly.
        self._ctx = self._context_provider.loop_context()

        # Initial gate: if no messages observed, nothing to do.
        if not await self._observe():
            return None

        self._set_active(True)

        actions_taken = 0
        self._consecutive = 0
        rsp = AIMessage(content=_NO_ACTIONS_YET, cause_by=CauseBy.ACTION)
        while actions_taken < self._ctx.max_react_loop:
            if await self._observe(max_priority=MessagePriority.NEXT):
                self._consecutive = 0
                self._set_active(True)
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
            actions_taken += 1
            self._consecutive += 1

            # post-check
            can_ask = "AskUserQuestion" in self._ctx.tools
            if self._ctx.max_react_loop >= 10 and actions_taken >= self._ctx.max_react_loop:
                if not can_ask:
                    break
                answer = await self._ask_user(
                    "I have reached my max action rounds, do you want me to continue?",
                    "Continue?",
                    [("Yes", "Continue working on the task."), ("No", "Stop here.")],
                )
                if "yes" in answer.lower():
                    actions_taken = 0
            if self._consecutive >= self._ctx.max_consecutive_react_limit:
                if not can_ask:
                    break
                memory = self._memory.get(k=self._ctx.memory_k)
                context = memory + [UserMessage(content=SUMMARIZE_STATUS_WHEN_CONSECUTIVE)]
                llm = await self._context_provider.resolve_llm(context)
                question = await llm.aask(context)
                answer = await self._ask_user(
                    question,
                    "Guidance?",
                    [("Continue", "Proceed as planned."), ("Adjust", "Provide different instructions.")],
                )
                await self._memory.add(
                    UserMessage(content="User's extra instruction: " + answer, cause_by=CauseBy.RUN_COMMAND)
                )
                self._consecutive = 0

        return rsp
