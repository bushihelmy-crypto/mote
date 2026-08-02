"""Inference engine for model calls, streaming, and dedup checking.

Symmetric counterpart to ToolExecutor on the Act side.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Callable, Optional
from uuid import uuid4

from mote.contracts.events.output import OutputSnapshotEvent, OutputSnapshotInvalidatedEvent
from mote.contracts.model.inference import (
    FinalizedGenerateRequest,
    FinalizedInferenceRequest,
    InferenceAttemptFence,
    InferenceResult,
    ResolvedInferenceTarget,
    TargetInvalidated,
)
from mote.contracts.model.invocation import CanonicalToolCall, ResponseMode
from mote.contracts.ports.model.inference import ModelInferencePort
from mote.kernel.inference.base import BaseInferenceEngine
from mote.kernel.output.snapshots import OutputSnapshotAccumulator
from mote.kernel.telemetry.context import current_trace_id

if TYPE_CHECKING:
    from mote.contracts.ports.conversation.message_store import MessageStore


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


class InferenceEngine(BaseInferenceEngine):
    """Encapsulates LLM think calls and streaming.

    Mirrors ToolExecutor on the Think side so that Role only orchestrates.
    """

    def __init__(
        self,
        memory: "MessageStore",
        config,
        *,
        inference_port: ModelInferencePort,
        snapshot_scope: Callable[[OutputSnapshotAccumulator | None], Any],
        output_observer: Callable[[OutputSnapshotEvent | OutputSnapshotInvalidatedEvent], None],
        reporter_factory: Callable[..., Any] | None = None,
    ):
        # No fixed LLM: the react loop resolves the per-request LLM via the
        # router and hands it to ``start`` each round, so one Role can think
        # against different models across (and within) tasks.
        self.target: ResolvedInferenceTarget | None = None
        self.model_call_id = ""
        self.memory = memory
        self.config = config
        self._reporter_factory = reporter_factory or _NullThoughtReporter
        self._inference_port = inference_port
        self._snapshot_scope = snapshot_scope
        self._output_observer = output_observer
        # The single output contract for one think round. Replaced wholesale by
        # each _run; callers read it through the `result` property.
        self.result: InferenceResult = InferenceResult()
        self._task: Optional[asyncio.Task] = None

    async def start(
        self,
        req,
        system_prompt,
        tool_specs=None,
        *,
        target: ResolvedInferenceTarget,
        model_call_id: str,
        resume: bool = False,
        output_binding=None,
        output_schema=None,
        output_run_id="",
        schema_fingerprint="",
        attempt: InferenceAttemptFence | None = None,
        protocol_fingerprint="",
        vocabulary_fingerprint="",
        tool_projection_fingerprint="",
        prompt_section_set_fingerprint="",
        request_fingerprint="",
    ):
        """Launch the background think task.

        Receives already-built prompts plus the ``llm`` the loop resolved (via
        the router) for this request, so there is no dependency on Role. When
        ``tool_specs`` is provided, the native tool-use channel is used
        (aask_tool); otherwise the XML text channel (aask) is used.
        """
        self.target = target
        self.model_call_id = model_call_id
        self._task = asyncio.create_task(
            self._run(
                target,
                req,
                system_prompt,
                tool_specs,
                output_binding,
                output_schema,
                output_run_id,
                schema_fingerprint,
                model_call_id,
                resume,
                attempt,
                protocol_fingerprint,
                vocabulary_fingerprint,
                tool_projection_fingerprint,
                prompt_section_set_fingerprint,
                request_fingerprint,
            )
        )

    async def _run(
        self,
        target: ResolvedInferenceTarget,
        req,
        system_prompt,
        tool_specs=None,
        output_binding=None,
        output_schema=None,
        output_run_id="",
        schema_fingerprint="",
        model_call_id="",
        resume=False,
        attempt=None,
        protocol_fingerprint="",
        vocabulary_fingerprint="",
        tool_projection_fingerprint="",
        prompt_section_set_fingerprint="",
        request_fingerprint="",
    ):
        """Background: LLM call. Produces a fresh InferenceResult."""
        # start() always assigns self.llm before creating this task, so it is
        # non-None here; capture it into a local to narrow away the Optional.
        content = ""
        tool_calls: tuple[CanonicalToolCall, ...] | None = None
        try:
            async with self._reporter_factory(enable_llm_stream=True) as reporter:
                await reporter.async_report({"type": "react"})
                is_native_schema = getattr(getattr(output_binding, "kind", None), "value", "") == "native_schema"
                accumulator = (
                    OutputSnapshotAccumulator(
                        run_id=output_run_id,
                        schema_fingerprint=schema_fingerprint,
                        observer=self._output_observer,
                    )
                    if is_native_schema
                    else None
                )
                with self._snapshot_scope(accumulator):
                    mode = (
                        ResponseMode.NATIVE_SCHEMA
                        if is_native_schema
                        else (ResponseMode.NATIVE_TOOLS if tool_specs is not None else ResponseMode.TEXT)
                    )
                    result = await self._inference_port.infer(
                        target,
                        FinalizedInferenceRequest(
                            model_call_id=model_call_id,
                            payload=FinalizedGenerateRequest(
                                messages=tuple(req),
                                task="interactive",
                                system_prompt=system_prompt,
                                tools=tuple(tool_specs or ()),
                                output_schema=output_schema if is_native_schema else None,
                                response_mode=mode,
                                stream=True,
                                resume=resume,
                                trace_id=current_trace_id() or "",
                            ),
                            protocol_fingerprint=protocol_fingerprint,
                            vocabulary_fingerprint=vocabulary_fingerprint,
                            tool_projection_fingerprint=tool_projection_fingerprint,
                            prompt_section_set_fingerprint=prompt_section_set_fingerprint,
                            request_fingerprint=request_fingerprint,
                        ),
                        attempt or InferenceAttemptFence(model_call_id, uuid4().hex, 1),
                    )
                    if isinstance(result, TargetInvalidated):
                        raise RuntimeError(result.reason)
                    content = result.content or ""
                    tool_calls = result.tool_calls
        finally:
            await self._inference_port.release(target)
        self.result = InferenceResult(content=content, tool_calls=tool_calls)

    def reinstate(self, result: InferenceResult) -> None:
        """Adopt a journal-recovered result without launching the LLM.

        The durable resume path (see BaseInferenceEngine.reinstate): set the result
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
