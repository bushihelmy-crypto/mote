"""Frozen workflow definitions and per-execution run ownership."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from pydantic import BaseModel

from mote.orchestration.workflows.control import WorkflowPause
from mote.orchestration.workflows.events import (
    ProgressSink,
    ProgressWriter,
    RunStarted,
    RunTerminal,
    reset_progress_writer,
    set_progress_writer,
)


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python")
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_deep_freeze(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool, bytes)):
        return value
    return repr(value)


@dataclass(frozen=True, slots=True)
class Succeeded:
    output: Any


@dataclass(frozen=True, slots=True)
class Failed:
    error: BaseException
    continuation: "WorkflowContinuation | None" = None


@dataclass(frozen=True, slots=True)
class Paused:
    reason: str
    continuation: "WorkflowContinuation"


@dataclass(frozen=True, slots=True)
class Cancelled:
    reason: str = "cancelled"
    continuation: "WorkflowContinuation | None" = None


@dataclass(frozen=True, slots=True)
class TimedOut:
    reason: str = "timed_out"
    continuation: "WorkflowContinuation | None" = None


WorkflowOutcome = Succeeded | Failed | Paused | Cancelled | TimedOut


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    run_id: str
    definition_id: str
    state: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class WorkflowContinuation:
    continuation_id: str
    definition: "WorkflowDefinition" = field(repr=False, compare=False)
    checkpoint: Any = field(repr=False, compare=False)
    run_state: Any = field(default=None, repr=False, compare=False)

    def resume(
        self,
        overrides: Mapping[str, Any] | None = None,
        *,
        from_nodes: tuple[str, ...] = (),
        skip_nodes: tuple[str, ...] = (),
    ) -> "WorkflowRun":
        checkpoint = copy.deepcopy(self.checkpoint)
        if overrides:
            if isinstance(checkpoint, BaseModel):
                valid = set(type(checkpoint).model_fields)
                unknown = set(overrides) - valid
                if unknown:
                    raise ValueError(f"unknown workflow override fields: {', '.join(sorted(unknown))}")
                for name, value in overrides.items():
                    setattr(checkpoint, name, value)
            else:
                checkpoint = {**dict(checkpoint), **overrides}
        return self.definition.start(
            {},
            checkpoint=checkpoint,
            run_state=self.run_state,
            from_nodes=from_nodes,
            skip_nodes=skip_nodes,
        )


@dataclass(frozen=True)
class WorkflowDefinition:
    definition_id: str
    version: int
    _graph: Any = field(repr=False, compare=False)

    def start(
        self,
        initial_input: dict[str, Any],
        *,
        checkpoint: Any = None,
        run_state: Any = None,
        from_nodes: tuple[str, ...] = (),
        skip_nodes: tuple[str, ...] = (),
        progress_writer: ProgressWriter | None = None,
        progress_sink: ProgressSink | None = None,
    ) -> "WorkflowRun":
        return WorkflowRun(
            self,
            dict(initial_input),
            checkpoint=checkpoint,
            run_state=run_state,
            from_nodes=from_nodes,
            skip_nodes=skip_nodes,
            progress_writer=progress_writer,
            progress_sink=progress_sink,
        )


class WorkflowRun:
    def __init__(
        self,
        definition: WorkflowDefinition,
        initial_input: dict[str, Any],
        *,
        checkpoint: Any = None,
        run_state: Any = None,
        from_nodes: tuple[str, ...] = (),
        skip_nodes: tuple[str, ...] = (),
        progress_writer: ProgressWriter | None = None,
        progress_sink: ProgressSink | None = None,
    ) -> None:
        self.run_id = uuid.uuid4().hex
        self.definition = definition
        self._initial_input = initial_input
        self._checkpoint = checkpoint
        self._run_state = run_state
        self._from_nodes = from_nodes
        self._skip_nodes = skip_nodes
        self._progress_writer = progress_writer
        self._progress_sink = progress_sink
        self._executing = False
        self._task: asyncio.Task | None = None
        self._state: Any = copy.deepcopy(checkpoint) if checkpoint is not None else dict(initial_input)
        self._status = "pending"

    def snapshot(self) -> RunSnapshot:
        return RunSnapshot(
            run_id=self.run_id,
            definition_id=self.definition.definition_id,
            state=_deep_freeze(self._state),
        )

    def continuation(self) -> WorkflowContinuation:
        return WorkflowContinuation(
            continuation_id=uuid.uuid4().hex,
            definition=self.definition,
            checkpoint=copy.deepcopy(self._state),
            run_state=self._run_state,
        )

    async def execute(self) -> WorkflowOutcome:
        if self._executing:
            raise RuntimeError("WorkflowRun.execute() already has an owner")
        self._executing = True
        token = set_progress_writer(self._progress_writer)
        self._task = asyncio.current_task()
        self._status = "running"
        if self._progress_sink is not None:
            await self._progress_sink.emit(RunStarted(run_id=self.run_id, definition_id=self.definition.definition_id))
        try:
            if self._checkpoint is None:
                output = await self.definition._graph.arun(
                    run_state=self._run_state,
                    **self._initial_input,
                )
            else:
                output = await self._resume_graph()
            if isinstance(output, WorkflowPause):
                self._state = output.state
                self._status = "paused"
                return Paused(output.reason.value, self.continuation())
            self._state = output
            self._status = "succeeded"
            return Succeeded(output)
        except asyncio.CancelledError:
            self._status = "cancelled"
            return Cancelled()
        except TimeoutError:
            self._status = "timed_out"
            return TimedOut(continuation=self.continuation())
        except BaseException as exc:  # noqa: BLE001
            graph_state = getattr(exc, "graph_state", None)
            if graph_state is not None:
                self._state = graph_state
            self._status = "failed"
            continuation = self.continuation() if graph_state is not None else None
            return Failed(exc, continuation)
        finally:
            if self._progress_sink is not None:
                await self._progress_sink.emit(RunTerminal(run_id=self.run_id, status=self._status))
            self._task = None
            reset_progress_writer(token)

    async def _resume_graph(self) -> Any:
        graph = self.definition._graph
        if self._skip_nodes and self._from_nodes:
            deferred = graph.resume_skip_and_from(
                state=self._checkpoint,
                skip_nodes=list(self._skip_nodes),
                from_nodes=list(self._from_nodes),
                run_state=self._run_state,
            )
        elif self._skip_nodes:
            deferred = graph.resume_skip(
                state=self._checkpoint,
                skip_nodes=list(self._skip_nodes),
                run_state=self._run_state,
            )
        elif self._from_nodes:
            deferred = graph.resume(
                state=self._checkpoint,
                from_nodes=list(self._from_nodes),
                run_state=self._run_state,
            )
        else:
            if isinstance(self._checkpoint, BaseModel):
                values = self._checkpoint.model_dump(mode="python")
            else:
                values = dict(self._checkpoint)
            return await graph.arun(run_state=self._run_state, **values)
        if deferred.poll_factory is None:
            raise RuntimeError("resumed workflow did not provide an execution factory")
        return await deferred.poll_factory()

    async def aclose(self) -> None:
        task = self._task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def freeze_builder(builder: Any) -> WorkflowDefinition:
    graph = copy.deepcopy(builder)
    graph._prepare()
    version = int(getattr(builder, "definition_version", 1))
    canonical = {
        "name": graph.command_name,
        "version": version,
        "nodes": sorted(graph._nodes),
        "edges": sorted((edge.from_node, edge.to_node) for edge in graph._edges),
        "waiting": sorted((edge.sources, edge.to_node) for edge in graph._waiting_edges),
        "conditional": sorted((edge.from_node, sorted(edge.mapping.items())) for edge in graph._conditional_edges),
        "decisions": sorted((edge.from_node, edge.prompt, sorted(edge.mapping.items())) for edge in graph._llm_edges),
    }
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[
        :16
    ]
    identity = f"workflow:{graph.command_name}:v{version}:{digest}"
    return WorkflowDefinition(identity, version, graph)


__all__ = [
    "Cancelled",
    "Failed",
    "Paused",
    "ProgressSink",
    "RunSnapshot",
    "Succeeded",
    "TimedOut",
    "WorkflowDefinition",
    "WorkflowContinuation",
    "WorkflowOutcome",
    "WorkflowRun",
]
