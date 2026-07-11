"""ThinkEngine — encapsulates LLM think calls, streaming, and dedup checking.

Symmetric counterpart to ToolExecutor on the Act side.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Optional

from mote.common.base import BaseThinkEngine
from mote.common.const import TOOL_CALLS
from mote.common.logs import log_class
from mote.common.schema import ThinkResult
from mote.common.utils.report import ThoughtReporter
from mote.common.utils.role_zero_utils import call_signature, check_duplicate_calls, check_duplicates

if TYPE_CHECKING:
    from mote.common.interface import LLMClient, MessageStore


@log_class(level="DEBUG")
class ThinkEngine(BaseThinkEngine):
    """Encapsulates LLM think call, streaming, and dedup check.

    Mirrors ToolExecutor on the Think side so that Role only orchestrates.
    """

    def __init__(self, memory: "MessageStore", config):
        # No fixed LLM: the react loop resolves the per-request LLM via the
        # router and hands it to ``start`` each round, so one Role can think
        # against different models across (and within) tasks.
        self.llm: Optional["LLMClient"] = None
        self.memory = memory
        self.config = config
        # The single output contract for one think round. Replaced wholesale by
        # each _run; callers read it through the `result` property.
        self.result: ThinkResult = ThinkResult()
        self._task: Optional[asyncio.Task] = None

    async def start(self, req, system_prompt, tool_specs=None, *, llm: "LLMClient"):
        """Launch the background think task.

        Receives already-built prompts plus the ``llm`` the loop resolved (via
        the router) for this request, so there is no dependency on Role. When
        ``tool_specs`` is provided, the native tool-use channel is used
        (aask_tool); otherwise the XML text channel (aask) is used.
        """
        self.llm = llm
        self._task = asyncio.create_task(self._run(req, system_prompt, tool_specs))

    async def _run(self, req, system_prompt, tool_specs=None):
        """Background: LLM call + dedup. Produces a fresh ThinkResult."""
        # start() always assigns self.llm before creating this task, so it is
        # non-None here; capture it into a local to narrow away the Optional.
        llm = self.llm
        assert llm is not None, "think task started before start() set the LLM"
        content = ""
        tool_calls: Optional[list[dict]] = None
        async with ThoughtReporter(enable_llm_stream=True) as reporter:
            await reporter.async_report({"type": "react"})
            if tool_specs:
                rsp = await llm.aask_tool(req, system_msgs=[system_prompt], tools=tool_specs)
                content = rsp.content or ""
                tool_calls = [{"id": c.id, "command_name": c.name, "args": c.arguments} for c in rsp.tool_calls]
            else:
                content = await llm.aask(req, system_msgs=[system_prompt])
        # Duplicate detection differs by protocol. XML compares raw response text;
        # native compares a structured-call signature (the text may be empty or
        # repeat while the calls differ), and on a hard repeat overrides the calls
        # with a synthesized ask_human call.
        if tool_calls is None:
            rsp_hist = [mem.content for mem in self.memory.get()]
            content = await check_duplicates(
                req=req,
                command_rsp=content,
                rsp_hist=rsp_hist,
                llm=llm,
            )
        else:
            sig_hist = [
                call_signature(mem.metadata[TOOL_CALLS]) for mem in self.memory.get() if mem.metadata.get(TOOL_CALLS)
            ]
            override = await check_duplicate_calls(
                req=req,
                command_calls=tool_calls,
                sig_hist=sig_hist,
                llm=llm,
            )
            if override is not None:
                tool_calls = override
        self.result = ThinkResult(content=content, tool_calls=tool_calls)

    async def join(self):
        """Await the current think task and clean up."""
        task = self._task
        self._task = None
        if task:
            await task

    @property
    def done(self) -> bool:
        """True if no task is pending or the task has finished."""
        return self._task is None or self._task.done()
