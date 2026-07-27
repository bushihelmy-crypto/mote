"""ThinkEngine — encapsulates LLM think calls, streaming, and dedup checking.

Symmetric counterpart to ToolExecutor on the Act side.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Callable, Optional

from mote.contracts.models.invocation import ResponseMode
from mote.contracts.think import ThinkResult
from mote.kernel.diagnostics import current_trace_id
from mote.kernel.models.model_calls import generate
from mote.kernel.output_stream import OutputSnapshotAccumulator, bind_output_snapshot_accumulator
from mote.kernel.think.base import BaseThinkEngine

if TYPE_CHECKING:
    from mote.contracts.ports import MessageStore, ModelRoute


class _NullThoughtReporter:
    """Standalone Kernel reporter used when no Runtime integration is injected."""

    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def async_report(self, *_args, **_kwargs) -> None:
        return None


class ThinkEngine(BaseThinkEngine):
    """Encapsulates LLM think calls and streaming.

    Mirrors ToolExecutor on the Think side so that Role only orchestrates.
    """

    def __init__(self, memory: "MessageStore", config, reporter_factory: Callable[..., object] | None = None):
        # No fixed LLM: the react loop resolves the per-request LLM via the
        # router and hands it to ``start`` each round, so one Role can think
        # against different models across (and within) tasks.
        self.model_route: Optional["ModelRoute"] = None
        self.memory = memory
        self.config = config
        self._reporter_factory = reporter_factory or _NullThoughtReporter
        # The single output contract for one think round. Replaced wholesale by
        # each _run; callers read it through the `result` property.
        self.result: ThinkResult = ThinkResult()
        self._task: Optional[asyncio.Task] = None

    async def start(
        self,
        req,
        system_prompt,
        tool_specs=None,
        *,
        model_route: "ModelRoute",
        model_call_id: str,
        resume: bool = False,
        output_binding=None,
        output_schema=None,
        output_run_id="",
        schema_fingerprint="",
    ):
        """Launch the background think task.

        Receives already-built prompts plus the ``llm`` the loop resolved (via
        the router) for this request, so there is no dependency on Role. When
        ``tool_specs`` is provided, the native tool-use channel is used
        (aask_tool); otherwise the XML text channel (aask) is used.
        """
        self.model_route = model_route
        self._task = asyncio.create_task(
            self._run(
                req,
                system_prompt,
                tool_specs,
                output_binding,
                output_schema,
                output_run_id,
                schema_fingerprint,
                model_call_id,
                resume,
            )
        )

    async def _run(
        self,
        req,
        system_prompt,
        tool_specs=None,
        output_binding=None,
        output_schema=None,
        output_run_id="",
        schema_fingerprint="",
        model_call_id="",
        resume=False,
    ):
        """Background: LLM call. Produces a fresh ThinkResult."""
        # start() always assigns self.llm before creating this task, so it is
        # non-None here; capture it into a local to narrow away the Optional.
        route = self.model_route
        assert route is not None, "think task started before start() set the model route"
        content = ""
        tool_calls: Optional[list[dict]] = None
        async with self._reporter_factory(enable_llm_stream=True) as reporter:
            await reporter.async_report({"type": "react"})
            is_native_schema = getattr(getattr(output_binding, "kind", None), "value", "") == "native_schema"
            accumulator = (
                OutputSnapshotAccumulator(
                    run_id=output_run_id,
                    schema_fingerprint=schema_fingerprint,
                )
                if is_native_schema
                else None
            )
            with bind_output_snapshot_accumulator(accumulator):
                mode = (
                    ResponseMode.NATIVE_SCHEMA
                    if is_native_schema
                    else (ResponseMode.NATIVE_TOOLS if tool_specs is not None else ResponseMode.TEXT)
                )
                output, _resolved = await generate(
                    route,
                    req,
                    model_call_id=model_call_id,
                    task="interactive",
                    system_prompt=system_prompt,
                    tools=tool_specs,
                    output_schema=output_schema if is_native_schema else None,
                    response_mode=mode,
                    stream=True,
                    resume=resume,
                    trace_id=current_trace_id() or "",
                )
                content = output.content or ""
                if tool_specs is not None:
                    tool_calls = [
                        {
                            "id": call.id,
                            "command_name": call.name,
                            "args": call.arguments,
                        }
                        for call in output.tool_calls
                    ]
        self.result = ThinkResult(content=content, tool_calls=tool_calls)

    def reinstate(self, result: ThinkResult) -> None:
        """Adopt a journal-recovered result without launching the LLM.

        The durable resume path (see BaseThinkEngine.reinstate): set the result
        the run journal memoized before the crash and leave ``_task`` None so
        ``done`` is True — the channel then reads this reinstated result instead
        of re-paying the model.
        """
        self.result = result
        self._task = None

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
