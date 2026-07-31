"""Stable contracts for notebook documents rendered by interactive surfaces."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field, JsonValue, model_validator

NOTEBOOK_MEDIA_TYPE = "application/vnd.mote.notebook+json"
NOTEBOOK_EXPORT_MIME_TYPE = "application/x-ipynb+json"
_SAFE_DISPLAY_MEDIA_TYPES = frozenset({"image/png", "text/plain"})
_MAX_DOCUMENT_CONTENT_CHARS = 16_777_216


@dataclass(frozen=True, slots=True)
class NotebookExportRepresentation:
    """One immutable Notebook export before publication policy is applied."""

    representation: str
    mime_type: str
    content: bytes = field(repr=False)
    suggested_name: str = ""


class NotebookOutput(BaseModel):
    """One bounded, frontend-safe Jupyter output."""

    output_type: Literal["stream", "execute_result", "display_data", "error"]
    name: Literal["stdout", "stderr"] | None = None
    text: str = Field(default="", max_length=1_048_576)
    data: dict[str, str] = Field(default_factory=dict)
    execution_count: int | None = Field(default=None, ge=0)
    ename: str = Field(default="", max_length=512)
    evalue: str = Field(default="", max_length=4096)
    traceback: list[str] = Field(default_factory=list, max_length=256)
    display_id: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def _validate_shape(self) -> "NotebookOutput":
        if set(self.data) - _SAFE_DISPLAY_MEDIA_TYPES:
            raise ValueError("notebook output contains an unsafe display media type")
        if any(len(value) > 5_592_408 for value in self.data.values()):
            raise ValueError("notebook display payload is too large")
        if any(len(line) > 65_536 for line in self.traceback):
            raise ValueError("notebook traceback line is too large")
        return self


class NotebookCell(BaseModel):
    """One code cell and the outputs produced by its execution."""

    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    cell_type: Literal["code"] = "code"
    source: str = Field(max_length=262_144)
    execution_count: int | None = Field(default=None, ge=0)
    outputs: list[NotebookOutput] = Field(default_factory=list, max_length=256)
    status: Literal["queued", "running", "complete", "error", "timed_out", "interrupted"] = "complete"
    origin: Literal["agent", "human"] = "agent"
    extensions: dict[str, JsonValue] = Field(default_factory=dict)


class NotebookInputRequest(BaseModel):
    """One kernel stdin request visible on the live notebook surface."""

    request_id: str = Field(min_length=1, max_length=256)
    cell_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    prompt: str = Field(default="", max_length=65_536)
    password: bool = False


class NotebookInputReply(BaseModel):
    """A fenced human reply to the notebook's current stdin request."""

    request_id: str = Field(min_length=1, max_length=256)
    value: str = Field(max_length=1_048_576)


class NotebookDocument(BaseModel):
    """A complete, bounded snapshot of a notebook surface."""

    schema_version: Literal["1"] = "1"
    ref: str = Field(min_length=1, max_length=512)
    revision: int = Field(default=0, ge=0)
    kernel_epoch: int = Field(default=0, ge=0)
    kernel_status: Literal["idle", "busy", "restarting", "stopped"] = "idle"
    cells: list[NotebookCell] = Field(default_factory=list, max_length=256)
    input_request: NotebookInputRequest | None = None
    truncated: bool = False
    extensions: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _unique_cell_ids(self) -> "NotebookDocument":
        ids = [cell.id for cell in self.cells]
        if len(ids) != len(set(ids)):
            raise ValueError("notebook cell ids must be unique")
        content_size = sum(len(cell.source) for cell in self.cells)
        content_size += sum(
            len(output.text)
            + sum(len(value) for value in output.data.values())
            + sum(len(line) for line in output.traceback)
            for cell in self.cells
            for output in cell.outputs
        )
        if self.input_request is not None:
            content_size += len(self.input_request.prompt)
        if content_size > _MAX_DOCUMENT_CONTENT_CHARS:
            raise ValueError("notebook document content is too large")
        return self


class NotebookExecuteInput(BaseModel):
    """Human-authored code submitted by a notebook window."""

    cell_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    source: str = Field(min_length=1, max_length=262_144)


__all__ = [
    "NOTEBOOK_EXPORT_MIME_TYPE",
    "NOTEBOOK_MEDIA_TYPE",
    "NotebookCell",
    "NotebookDocument",
    "NotebookExecuteInput",
    "NotebookExportRepresentation",
    "NotebookInputReply",
    "NotebookInputRequest",
    "NotebookOutput",
]
