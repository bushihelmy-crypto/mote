"""ThinkEngine — encapsulates LLM think calls, streaming, and dedup checking.

Symmetric counterpart to ToolExecutor on the Act side.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Optional

from metagpt.common.schema import ThinkResult
from metagpt.common.logs import logger
from metagpt.common.base import BaseThinkEngine
from metagpt.common.utils.report import ThoughtReporter
from metagpt.common.utils.role_zero_utils import (
    call_signature,
    check_duplicate_calls,
    check_duplicates,
)

from metagpt.common.const import TOOL_CALLS

if TYPE_CHECKING:
    from metagpt.common.interface import LLMClient, MessageStore


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

    async def start(self, req, system_prompt, state_data, tool_specs=None, *, llm: "LLMClient"):
        """Launch the background think task.

        Receives already-built prompts plus the ``llm`` the loop resolved (via
        the router) for this request, so there is no dependency on Role. When
        ``tool_specs`` is provided, the native tool-use channel is used
        (aask_tool); otherwise the XML text channel (aask) is used.
        """
        self.llm = llm
        self._task = asyncio.create_task(
            self._run(req, system_prompt, state_data, tool_specs)
        )

    async def _run(self, req, system_prompt, state_data, tool_specs=None):
        """Background: LLM call + dedup. Produces a fresh ThinkResult."""
        content = ""
        tool_calls: Optional[list[dict]] = None
        async with ThoughtReporter(enable_llm_stream=True) as reporter:
            await reporter.async_report({"type": "react"})
            if tool_specs:
                rsp = await self._cached_aask_tool(
                    req=req, system_msgs=[system_prompt], tool_specs=tool_specs, state_data=state_data
                )
                content = rsp.content or ""
                tool_calls = [
                    {"id": c.id, "command_name": c.name, "args": c.arguments} for c in rsp.tool_calls
                ]
            else:
                content = await self._cached_aask(
                    req=req, system_msgs=[system_prompt], state_data=state_data
                )
        logger.info(f"Command response:\n{content}")
        # Duplicate detection differs by protocol. XML compares raw response text;
        # native compares a structured-call signature (the text may be empty or
        # repeat while the calls differ), and on a hard repeat overrides the calls
        # with a synthesized ask_human call.
        if tool_calls is None:
            rsp_hist = [mem.content for mem in self.memory.get()]
            content = await check_duplicates(
                req=req, command_rsp=content,
                rsp_hist=rsp_hist, llm=self.llm,
            )
        else:
            sig_hist = [
                call_signature(mem.metadata[TOOL_CALLS])
                for mem in self.memory.get()
                if mem.metadata.get(TOOL_CALLS)
            ]
            override = await check_duplicate_calls(
                req=req, command_calls=tool_calls,
                sig_hist=sig_hist, llm=self.llm,
            )
            if override is not None:
                tool_calls = override
        self.result = ThinkResult(content=content, tool_calls=tool_calls)

    async def _cached_aask(self, *, req, system_msgs, **kwargs):
        return await self.llm.aask(req, system_msgs=system_msgs)

    async def _cached_aask_tool(self, *, req, system_msgs, tool_specs, **kwargs):
        return await self.llm.aask_tool(req, system_msgs=system_msgs, tools=tool_specs)

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
