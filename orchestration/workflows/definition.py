"""Frozen workflow definitions and per-execution run ownership."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Mapping, cast, get_args, get_origin

from pydantic import BaseModel

from mote.contracts.events.envelope import JsonValue
from mote.contracts.workflow.identity import WorkflowDefinitionId
from mote.kernel.output import OutputContract
from mote.orchestration.workflows.control import WorkflowPause
from mote.orchestration.workflows.engine import _build_executor, _run_driver
from mote.orchestration.workflows.events import (
    ActivityRunStarted,
    ActivityRunTerminal,
    ProgressEventSink,
    ProgressSink,
    WorkflowCheckpointSink,
    reset_checkpoint_sink,
    reset_progress_sink,
    set_checkpoint_sink,
    set_progress_sink,
)
from mote.orchestration.workflows.types import (
    GraphPause,
    GraphRunState,
    GraphState,
    NodeRecord,
    WorkflowDeferredExecutor,
    WorkflowNodeStatus,
)

if TYPE_CHECKING:
    from mote.orchestration.workflows.graph import WorkflowBuilder


def _deep_freeze(value: object) -> object:
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
    raise TypeError(
        "workflow snapshot contains an unencoded value: " f"{type(value).__module__}.{type(value).__qualname__}"
    )


@dataclass(frozen=True, slots=True)
class Succeeded:
    output: "GraphState"


@dataclass(frozen=True, slots=True)
class Failed:
    error: BaseException


@dataclass(frozen=True, slots=True)
class Paused:
    reason: str


@dataclass(frozen=True, slots=True)
class Cancelled:
    reason: str = "cancelled"


@dataclass(frozen=True, slots=True)
class TimedOut:
    reason: str = "timed_out"


WorkflowOutcome = Succeeded | Failed | Paused | Cancelled | TimedOut


def _reject_non_finite_json(value: str) -> object:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    execution_id: str
    definition_id: WorkflowDefinitionId
    state: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    schema_version: str
    definition_id: WorkflowDefinitionId
    definition_version: int
    digest: str
    canonical_payload: str

    def __post_init__(self) -> None:
        if self.schema_version != "mote.workflow-definition/v1":
            raise ValueError("unknown WorkflowDefinition schema version")
        if type(self.definition_version) is not int or self.definition_version < 1:
            raise ValueError("WorkflowDefinition version must be a positive integer")
        expected = hashlib.sha256(self.canonical_payload.encode("utf-8")).hexdigest()
        if self.digest != expected:
            raise ValueError("WorkflowDefinition digest mismatch")
        if self.definition_id != f"mote.workflow.v1.sha256-{expected}":
            raise ValueError("WorkflowDefinition identity does not match its payload")
        try:
            payload = json.loads(self.canonical_payload, parse_constant=_reject_non_finite_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("WorkflowDefinition payload is not canonical JSON") from exc
        if (
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            != self.canonical_payload
        ):
            raise ValueError("WorkflowDefinition payload is not canonically encoded")
        if type(payload) is not dict or set(payload) != {
            "schema_version",
            "definition_version",
            "name",
            "state_schema",
            "nodes",
            "edges",
            "waiting_edges",
            "conditional_edges",
            "llm_edges",
            "policy",
        }:
            raise ValueError("WorkflowDefinition payload has an invalid shape")
        if payload["schema_version"] != self.schema_version:
            raise ValueError("WorkflowDefinition payload schema version mismatch")
        if type(payload["definition_version"]) is not int or payload["definition_version"] != self.definition_version:
            raise ValueError("WorkflowDefinition payload version mismatch")
        if type(payload["name"]) is not str or not payload["name"]:
            raise ValueError("WorkflowDefinition name is invalid")
        nodes = payload["nodes"]
        if type(nodes) is not list or not nodes:
            raise ValueError("WorkflowDefinition nodes are invalid")
        for node in nodes:
            if type(node) is not dict or set(node) != {
                "name",
                "implementation_id",
                "description",
                "params",
                "timeout",
            }:
                raise ValueError("WorkflowDefinition node shape is invalid")
            if type(node["name"]) is not str or not node["name"]:
                raise ValueError("WorkflowDefinition node name is invalid")
            if type(node["implementation_id"]) is not str or not node["implementation_id"]:
                raise ValueError("WorkflowDefinition implementation identity is invalid")
        for edge in payload["conditional_edges"]:
            if type(edge) is not dict or set(edge) != {"from", "implementation_id", "mapping"}:
                raise ValueError("WorkflowDefinition conditional edge shape is invalid")
            if type(edge["implementation_id"]) is not str or not edge["implementation_id"]:
                raise ValueError("WorkflowDefinition router identity is invalid")


@dataclass(frozen=True, slots=True)
class WorkflowExecutable:
    """Process-local executable bound to one validated durable definition."""

    definition: WorkflowDefinition
    _graph: "WorkflowBuilder" = field(repr=False, compare=False)

    def start(
        self,
        initial_input: Mapping[str, object],
        *,
        checkpoint: "GraphState | None" = None,
        run_state: GraphRunState | None = None,
        from_nodes: tuple[str, ...] = (),
        skip_nodes: tuple[str, ...] = (),
        progress_sink_binding: ProgressEventSink | None = None,
        progress_sink: ProgressSink | None = None,
        checkpoint_sink: WorkflowCheckpointSink | None = None,
    ) -> "WorkflowRun":
        return WorkflowRun(
            self,
            dict(initial_input),
            checkpoint=checkpoint,
            run_state=run_state,
            from_nodes=from_nodes,
            skip_nodes=skip_nodes,
            progress_sink_binding=progress_sink_binding,
            progress_sink=progress_sink,
            checkpoint_sink=checkpoint_sink,
        )

    def compile(self) -> WorkflowDeferredExecutor:
        """Create the deferred executor from this canonical definition."""
        return _build_executor(self._graph)

    def restore_checkpoint(self, payload: str) -> tuple["GraphState", GraphRunState]:
        try:
            raw = json.loads(payload, parse_constant=_reject_non_finite_json)
        except json.JSONDecodeError as exc:
            raise ValueError("durable Workflow checkpoint is not JSON") from exc
        if (
            json.dumps(
                raw,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            != payload
        ):
            raise ValueError("durable Workflow checkpoint is not canonical JSON")
        if type(raw) is not dict or set(raw) != {"schema", "state", "run_state"}:
            raise ValueError("durable Workflow checkpoint envelope is invalid")
        if raw["schema"] != "mote.workflow-checkpoint/v2":
            raise ValueError("durable Workflow checkpoint schema is unknown")
        if type(raw["state"]) is not dict or type(raw["run_state"]) is not dict:
            raise ValueError("durable Workflow checkpoint payload is invalid")
        run_raw = raw["run_state"]
        if set(run_raw) != {"records", "activity_execution_id"}:
            raise ValueError("durable Workflow run-state shape is invalid")
        if type(run_raw["activity_execution_id"]) is not str or not run_raw["activity_execution_id"]:
            raise ValueError("durable Workflow run-state identity is invalid")
        records_raw = run_raw["records"]
        if type(records_raw) is not dict:
            raise ValueError("durable Workflow node records are invalid")
        record_fields = {
            "name",
            "status",
            "attempts",
            "last_error",
            "started_at",
            "ended_at",
            "last_route_key",
            "writes",
            "retries_attempted",
            "retries_limit",
        }
        records: dict[str, NodeRecord] = {}
        for node_name, item in records_raw.items():
            if (
                type(node_name) is not str
                or type(item) is not dict
                or set(item) != record_fields
                or item["name"] != node_name
            ):
                raise ValueError("durable Workflow node record shape is invalid")
            if (
                type(item["status"]) is not str
                or type(item["attempts"]) is not int
                or item["attempts"] < 0
                or type(item["retries_attempted"]) is not int
                or item["retries_attempted"] < 0
                or type(item["retries_limit"]) is not int
                or item["retries_limit"] < 0
                or type(item["writes"]) is not list
                or any(type(value) is not str for value in item["writes"])
                or any(
                    value is not None and type(value) is not str
                    for value in (item["last_error"], item["last_route_key"])
                )
                or any(
                    value is not None and type(value) not in {int, float}
                    for value in (item["started_at"], item["ended_at"])
                )
            ):
                raise ValueError("durable Workflow node record value is invalid")
            records[node_name] = NodeRecord(
                name=node_name,
                status=WorkflowNodeStatus(item["status"]),
                attempts=item["attempts"],
                last_error=item["last_error"],
                started_at=item["started_at"],
                ended_at=item["ended_at"],
                last_route_key=item["last_route_key"],
                writes=list(item["writes"]),
                retries_attempted=item["retries_attempted"],
                retries_limit=item["retries_limit"],
            )
        if set(records) != set(self._graph._nodes):
            raise ValueError("durable Workflow checkpoint definition mismatch")
        return (
            self._graph.state_schema(**raw["state"]),
            GraphRunState(
                records=records,
                activity_execution_id=run_raw["activity_execution_id"],
            ),
        )

    def encode_checkpoint(self, state: "GraphState", run_state: GraphRunState) -> str:
        state_value = state.model_dump(mode="python")
        return json.dumps(
            {
                "schema": "mote.workflow-checkpoint/v2",
                "state": _canonical_value(state_value, path="checkpoint.state"),
                "run_state": _canonical_value(asdict(run_state), path="checkpoint.run_state"),
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    async def arun(
        self,
        *,
        run_state: GraphRunState | None = None,
        **initial_state: object,
    ) -> "GraphState | GraphPause":
        """Execute this definition inline without creating a durable run owner."""
        state = self._graph.state_schema(**initial_state)
        run_state = run_state or GraphRunState.for_graph(self._graph)
        terminal = await _run_driver(
            self._graph,
            state,
            execute_nodes=self._graph._get_entry_nodes(),
            completed=set(),
            trigger_count=defaultdict(set),
            initial_params=dict(initial_state),
            run_state=run_state,
        )
        return terminal if isinstance(terminal, GraphPause) else state


class WorkflowRun:
    def __init__(
        self,
        executable: WorkflowExecutable,
        initial_input: Mapping[str, object],
        *,
        checkpoint: "GraphState | None" = None,
        run_state: GraphRunState | None = None,
        from_nodes: tuple[str, ...] = (),
        skip_nodes: tuple[str, ...] = (),
        progress_sink_binding: ProgressEventSink | None = None,
        progress_sink: ProgressSink | None = None,
        checkpoint_sink: WorkflowCheckpointSink | None = None,
    ) -> None:
        self.execution_id = "wfx_" + uuid.uuid4().hex
        self.executable = executable
        self._initial_input = dict(initial_input)
        self._checkpoint = checkpoint
        self._run_state = run_state
        self._from_nodes = from_nodes
        self._skip_nodes = skip_nodes
        self._progress_sink_binding = progress_sink_binding
        self._progress_sink = progress_sink
        self._checkpoint_sink = checkpoint_sink
        self._executing = False
        self._task: asyncio.Task | None = None
        self._state: GraphState | Mapping[str, object] = (
            copy.deepcopy(checkpoint) if checkpoint is not None else dict(initial_input)
        )
        self._status = "pending"

    def snapshot(self) -> RunSnapshot:
        state = _deep_freeze(self._state)
        if not isinstance(state, Mapping):
            raise TypeError("Workflow run snapshot state must be an object")
        return RunSnapshot(
            execution_id=self.execution_id,
            definition_id=self.executable.definition.definition_id,
            state=cast(Mapping[str, object], state),
        )

    def bind_checkpoint_sink(self, sink: WorkflowCheckpointSink) -> None:
        if self._executing or self._checkpoint_sink is not None:
            raise RuntimeError("Workflow checkpoint sink is already bound")
        self._checkpoint_sink = sink

    def bind_progress_sink(self, sink: ProgressEventSink) -> None:
        if self._executing or self._progress_sink_binding is not None:
            raise RuntimeError("Workflow progress sink is already bound")
        self._progress_sink_binding = sink

    async def execute(self) -> WorkflowOutcome:
        if self._executing:
            raise RuntimeError("WorkflowRun.execute() already has an owner")
        self._executing = True
        token = set_progress_sink(self._progress_sink_binding)
        checkpoint_token = set_checkpoint_sink(self._checkpoint_sink)
        self._task = asyncio.current_task()
        self._status = "running"
        if self._progress_sink is not None:
            await self._progress_sink.emit(
                ActivityRunStarted(
                    activity_run_id=self.execution_id,
                    activity_definition_id=str(self.executable.definition.definition_id),
                )
            )
        try:
            if self._checkpoint is None:
                output = await self.executable.arun(
                    run_state=self._run_state,
                    **self._initial_input,
                )
            else:
                output = await self._resume_graph()
            if isinstance(output, WorkflowPause):
                self._state = output.state
                self._status = "paused"
                return Paused(output.reason.value)
            self._state = output
            self._status = "succeeded"
            return Succeeded(output)
        except asyncio.CancelledError:
            self._status = "cancelled"
            return Cancelled()
        except TimeoutError:
            self._status = "timed_out"
            return TimedOut()
        except BaseException as exc:  # noqa: BLE001
            graph_state = getattr(exc, "graph_state", None)
            if graph_state is not None:
                self._state = graph_state
            self._status = "failed"
            return Failed(exc)
        finally:
            if self._progress_sink is not None:
                await self._progress_sink.emit(
                    ActivityRunTerminal(activity_run_id=self.execution_id, status=self._status)
                )
            self._task = None
            reset_progress_sink(token)
            reset_checkpoint_sink(checkpoint_token)

    async def _resume_graph(self) -> "GraphState | GraphPause":
        graph = self.executable._graph
        checkpoint = self._checkpoint
        if checkpoint is None:
            raise RuntimeError("Workflow resume requires a checkpoint")
        if self._skip_nodes and self._from_nodes:
            deferred = graph.resume_skip_and_from(
                state=checkpoint,
                skip_nodes=list(self._skip_nodes),
                from_nodes=list(self._from_nodes),
                run_state=self._run_state,
            )
        elif self._skip_nodes:
            deferred = graph.resume_skip(
                state=checkpoint,
                skip_nodes=list(self._skip_nodes),
                run_state=self._run_state,
            )
        elif self._from_nodes:
            deferred = graph.resume(
                state=checkpoint,
                from_nodes=list(self._from_nodes),
                run_state=self._run_state,
            )
        else:
            values = checkpoint.model_dump(mode="python")
            return await graph.arun(run_state=self._run_state, **values)
        if deferred.poll_factory is None:
            raise RuntimeError("resumed workflow did not provide an execution factory")
        return await deferred.poll_factory()

    async def aclose(self) -> None:
        task = self._task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


_PRIMITIVE_SCHEMA_IDENTITIES: Mapping[type[object], str] = MappingProxyType(
    {
        str: "mote.schema.primitive/string/v1",
        int: "mote.schema.primitive/integer/v1",
        float: "mote.schema.primitive/number/v1",
        bool: "mote.schema.primitive/boolean/v1",
    }
)


def _canonical_type(value: object, *, path: str) -> dict[str, JsonValue]:
    if isinstance(value, type) and value in _PRIMITIVE_SCHEMA_IDENTITIES:
        return {"schema_id": _PRIMITIVE_SCHEMA_IDENTITIES[value]}
    origin = get_origin(value)
    if origin is list:
        arguments = get_args(value)
        if len(arguments) != 1:
            raise ValueError(f"{path} list type must declare exactly one item type")
        return {
            "schema_id": "mote.schema.collection/list/v1",
            "items": _canonical_type(arguments[0], path=f"{path}.items"),
        }
    raise ValueError(f"{path} type requires an explicit versioned schema identity")


def _canonical_value(value: object, *, path: str) -> JsonValue:
    if value is None or type(value) in (str, int, bool):
        return cast(JsonValue, value)
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite float")
        return value
    if type(value) is bytes:
        return {"bytes_hex": value.hex()}
    if value is Ellipsis:
        return {"literal": "ellipsis"}
    if isinstance(value, Enum):
        return _canonical_value(value.value, path=path)
    if isinstance(value, BaseModel):
        return _canonical_value(value.model_dump(mode="python"), path=path)
    if isinstance(value, OutputContract):
        if value.migration_registry is not None or value.validator_migration_registry is not None:
            raise ValueError(f"{path} migration registries require a stable Product catalog identity")
        return {
            "contract_id": {
                "namespace": value.contract_id.namespace,
                "name": value.contract_id.name,
                "version": value.contract_id.version,
            },
            "schema": _canonical_value(value.decoder.schema.canonical, path=f"{path}.schema"),
            "schema_fingerprint": value.decoder.schema.fingerprint,
            "retry_max_corrections": value.retry_policy.max_corrections,
            "validators": [
                {
                    "name": validator.name,
                    "version": validator.version,
                    "stage": validator.stage.value,
                    "determinism": validator.determinism.value,
                    "effect": validator.effect.value,
                }
                for validator in value.validators
            ],
        }
    if isinstance(value, type) or get_origin(value) is not None:
        return _canonical_type(value, path=path)
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ValueError(f"{path} mapping keys must be strings")
        mapping = cast(Mapping[str, object], value)
        return {key: _canonical_value(mapping[key], path=f"{path}.{key}") for key in sorted(mapping)}
    if isinstance(value, (tuple, list)):
        return [_canonical_value(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, (set, frozenset)):
        encoded = [_canonical_value(item, path=f"{path}[]") for item in value]
        return sorted(encoded, key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
    if callable(value):
        raise ValueError(f"{path} callable requires an explicit versioned implementation identity")
    raise ValueError(
        f"{path} contains unencoded {type(value).__module__}.{type(value).__qualname__}; "
        "register a stable implementation identity"
    )


def _implementation_id(value: object, explicit: str, *, path: str) -> str:
    if explicit:
        if not explicit.strip() or explicit != explicit.strip():
            raise ValueError(f"{path} implementation identity is invalid")
        return explicit
    raise ValueError(f"{path} requires an explicit versioned implementation identity")


class WorkflowDefinitionCompiler:
    """The sole compiler from a mutable graph declaration to an immutable definition."""

    schema_version = "mote.workflow-definition/v1"

    @classmethod
    def compile(cls, builder: "WorkflowBuilder") -> WorkflowExecutable:
        graph = copy.deepcopy(builder)
        graph._prepare()
        version = graph.definition_version
        state_schema = graph.state_schema.model_json_schema()
        nodes = [
            {
                "name": name,
                "implementation_id": _implementation_id(
                    node.fn,
                    node.implementation_id,
                    path=f"nodes.{name}",
                ),
                "description": node.description,
                "params": _canonical_value(node.params, path=f"nodes.{name}.params"),
                "timeout": _canonical_value(
                    getattr(getattr(node.fn, "__self__", None), "timeout", None),
                    path=f"nodes.{name}.timeout",
                ),
            }
            for name, node in sorted(graph._nodes.items())
        ]
        canonical = {
            "schema_version": cls.schema_version,
            "definition_version": version,
            "name": graph.command_name,
            "state_schema": state_schema,
            "nodes": nodes,
            "edges": sorted((edge.from_node, edge.to_node) for edge in graph._edges),
            "waiting_edges": sorted((sorted(edge.sources), edge.to_node) for edge in graph._waiting_edges),
            "conditional_edges": [
                {
                    "from": edge.from_node,
                    "implementation_id": _implementation_id(
                        edge.router,
                        edge.implementation_id,
                        path=f"conditional.{edge.from_node}",
                    ),
                    "mapping": dict(sorted(edge.mapping.items())),
                }
                for edge in sorted(graph._conditional_edges, key=lambda item: item.from_node)
            ],
            "llm_edges": [
                {
                    "from": edge.from_node,
                    "prompt": edge.prompt,
                    "mapping": dict(sorted(edge.mapping.items())),
                }
                for edge in sorted(graph._llm_edges, key=lambda item: item.from_node)
            ],
            "policy": {
                "max_restarts": graph.max_restarts,
                "recursion_limit": graph.recursion_limit,
                "retry_policy": "workflow-engine-exponential/v1",
                "no_output": graph._no_output,
                "output_fields": sorted(graph._output_fields),
                "output_contract": _canonical_value(
                    graph.output_contract,
                    path="output_contract",
                ),
                "output_engine_factory": (
                    _implementation_id(
                        graph.output_engine_factory,
                        graph.output_engine_identity,
                        path="output_engine_factory",
                    )
                    if graph.output_engine_factory is not None
                    else None
                ),
            },
        }
        payload = json.dumps(
            canonical,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        graph._definition_id = WorkflowDefinitionId(f"mote.workflow.v1.sha256-{digest}")
        definition = WorkflowDefinition(
            cls.schema_version,
            graph._definition_id,
            version,
            digest,
            payload,
        )
        return WorkflowExecutable(definition, graph)


__all__ = [
    "Cancelled",
    "Failed",
    "Paused",
    "ProgressSink",
    "RunSnapshot",
    "Succeeded",
    "TimedOut",
    "WorkflowDefinition",
    "WorkflowExecutable",
    "WorkflowDefinitionCompiler",
    "WorkflowOutcome",
    "WorkflowRun",
]
