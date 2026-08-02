"""Strict source-tagged status projection shared across task boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

STATUS_PROJECTION_SCHEMA = "mote.execution-status/v1"


class ExecutionStatusSource(str, Enum):
    BACKGROUND_TASK = "background_task"
    WORKFLOW_NODE = "workflow_node"


@dataclass(frozen=True, slots=True)
class ExecutionStatusProjection:
    source: ExecutionStatusSource
    value: str

    def __post_init__(self) -> None:
        if not self.value:
            raise ValueError("execution status value must not be empty")

    def to_payload(self) -> dict[str, str]:
        return {
            "schema": STATUS_PROJECTION_SCHEMA,
            "source": self.source.value,
            "value": self.value,
        }

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, object],
        *,
        expected_source: ExecutionStatusSource,
    ) -> "ExecutionStatusProjection":
        if set(payload) != {"schema", "source", "value"}:
            raise ValueError("execution status projection has unsupported fields")
        if payload["schema"] != STATUS_PROJECTION_SCHEMA:
            raise ValueError("execution status projection has unsupported schema")
        source = payload["source"]
        value = payload["value"]
        if not isinstance(source, str) or not isinstance(value, str):
            raise TypeError("execution status source and value must be strings")
        decoded_source = ExecutionStatusSource(source)
        if decoded_source is not expected_source:
            raise ValueError("execution status source does not match its owner")
        return cls(decoded_source, value)


__all__ = [
    "ExecutionStatusProjection",
    "ExecutionStatusSource",
    "STATUS_PROJECTION_SCHEMA",
]
