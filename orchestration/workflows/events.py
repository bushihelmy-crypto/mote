"""Workflow progress facts emitted through a run-scoped sink."""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeAlias

from mote.runtime.events.progress_scope import (
    ProgressWriter,
    bind_progress_writer,
    current_progress_writer,
    reset_progress_writer,
)
from mote.runtime.telemetry.logging import logger


@dataclass(frozen=True, slots=True)
class RunStarted:
    type: Literal["run_started"] = "run_started"
    schema_version: int = 1
    run_id: str = ""
    definition_id: str = ""


@dataclass(frozen=True, slots=True)
class NodeStarted:
    type: Literal["node_started"] = "node_started"
    schema_version: int = 1
    run_id: str = ""
    node_id: str = ""
    attempt: int = 1


@dataclass(frozen=True, slots=True)
class NodeSucceeded:
    type: Literal["node_succeeded"] = "node_succeeded"
    schema_version: int = 1
    run_id: str = ""
    node_id: str = ""


@dataclass(frozen=True, slots=True)
class NodeFailed:
    type: Literal["node_failed"] = "node_failed"
    schema_version: int = 1
    run_id: str = ""
    node_id: str = ""
    error_code: str = "workflow_node_failed"


@dataclass(frozen=True, slots=True)
class RunTerminal:
    type: Literal["run_terminal"] = "run_terminal"
    schema_version: int = 1
    run_id: str = ""
    status: str = ""


WorkflowEvent: TypeAlias = RunStarted | NodeStarted | NodeSucceeded | NodeFailed | RunTerminal


class ProgressSink(Protocol):
    async def emit(self, event: WorkflowEvent) -> None:
        ...


def report_progress(stage: str, status: Any, detail: Any = None) -> None:
    writer = current_progress_writer()
    if writer is None:
        return
    try:
        writer(stage, status, detail)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"workflow progress sink failed: {exc}")


def set_progress_writer(writer: ProgressWriter | None) -> contextvars.Token:
    return bind_progress_writer(writer)


def _as_text(text: Any) -> str:
    return str(text) if text is not None else ""


__all__ = [
    "NodeFailed",
    "NodeStarted",
    "NodeSucceeded",
    "ProgressSink",
    "ProgressWriter",
    "RunStarted",
    "RunTerminal",
    "WorkflowEvent",
    "report_progress",
    "reset_progress_writer",
    "set_progress_writer",
]
