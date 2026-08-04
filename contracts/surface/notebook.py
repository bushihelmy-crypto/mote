"""Stable contracts for notebook documents rendered by interactive surfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

NOTEBOOK_MEDIA_TYPE = "application/vnd.mote.notebook+json"
NOTEBOOK_EXPORT_MIME_TYPE = "application/x-ipynb+json"
_SAFE_DISPLAY_MEDIA_TYPES = frozenset({"image/png", "text/plain"})
_MAX_DOCUMENT_CONTENT_CHARS = 16_777_216


class _StrictNotebookModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True, slots=True)
class NotebookExportRepresentation:
    """One immutable Notebook export before publication policy is applied."""

    representation: str
    mime_type: str
    content: bytes = field(repr=False)
    suggested_name: str = ""


class NotebookStreamOutput(_StrictNotebookModel):
    output_type: Literal["stream"] = "stream"
    name: Literal["stdout", "stderr"] = "stdout"
    text: str = Field(default="", max_length=1_048_576)


class _NotebookDisplayOutput(_StrictNotebookModel):
    data: dict[str, str] = Field(default_factory=dict)
    display_id: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def _validate_media(self) -> "_NotebookDisplayOutput":
        if set(self.data) - _SAFE_DISPLAY_MEDIA_TYPES:
            raise ValueError("notebook output contains an unsafe display media type")
        if any(len(value) > 5_592_408 for value in self.data.values()):
            raise ValueError("notebook display payload is too large")
        return self


class NotebookExecuteResultOutput(_NotebookDisplayOutput):
    output_type: Literal["execute_result"] = "execute_result"
    execution_count: int | None = Field(default=None, ge=0)


class NotebookDisplayDataOutput(_NotebookDisplayOutput):
    output_type: Literal["display_data"] = "display_data"


class NotebookErrorOutput(_StrictNotebookModel):
    output_type: Literal["error"] = "error"
    ename: str = Field(default="", max_length=512)
    evalue: str = Field(default="", max_length=4096)
    traceback: list[str] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def _validate_traceback(self) -> "NotebookErrorOutput":
        if any(len(line) > 65_536 for line in self.traceback):
            raise ValueError("notebook traceback line is too large")
        return self


NotebookOutput: TypeAlias = Annotated[
    NotebookStreamOutput | NotebookExecuteResultOutput | NotebookDisplayDataOutput | NotebookErrorOutput,
    Field(discriminator="output_type"),
]


def _output_content_size(output: NotebookOutput) -> int:
    if isinstance(output, NotebookStreamOutput):
        return len(output.text)
    if isinstance(output, NotebookErrorOutput):
        return sum(len(line) for line in output.traceback)
    return sum(len(value) for value in output.data.values())


class NotebookCell(_StrictNotebookModel):
    """One code cell and the outputs produced by its execution."""

    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    cell_type: Literal["code"] = "code"
    source: str = Field(max_length=262_144)
    execution_count: int | None = Field(default=None, ge=0)
    outputs: list[NotebookOutput] = Field(default_factory=list, max_length=256)
    status: Literal["queued", "running", "complete", "error", "timed_out", "interrupted"] = "complete"
    origin: Literal["agent", "human"] = "agent"
    extensions: dict[str, JsonValue] = Field(default_factory=dict)


class NotebookInputRequest(_StrictNotebookModel):
    """One kernel stdin request visible on the live notebook surface."""

    request_id: str = Field(min_length=1, max_length=256)
    request_revision: int = Field(default=1, ge=1)
    cell_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    prompt: str = Field(default="", max_length=65_536)
    password: bool = False
    document_revision: int = Field(default=0, ge=0)
    kernel_epoch: int = Field(default=0, ge=0)
    connection_generation: int = Field(default=0, ge=0)
    human_generation: int = Field(default=0, ge=0)


class NotebookInputReply(_StrictNotebookModel):
    """A fenced human reply to the notebook's current stdin request."""

    request_id: str = Field(min_length=1, max_length=256)
    value: str = Field(max_length=1_048_576)
    document_revision: int = Field(ge=0)
    kernel_epoch: int = Field(ge=0)
    connection_generation: int = Field(ge=1)
    human_generation: int = Field(ge=1)
    expected_request_revision: int = Field(ge=1)


class NotebookDocument(_StrictNotebookModel):
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
        content_size += sum(_output_content_size(output) for cell in self.cells for output in cell.outputs)
        if self.input_request is not None:
            content_size += len(self.input_request.prompt)
        if content_size > _MAX_DOCUMENT_CONTENT_CHARS:
            raise ValueError("notebook document content is too large")
        return self


class NotebookExecuteInput(_StrictNotebookModel):
    """Human-authored code submitted by a notebook window."""

    cell_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    source: str = Field(min_length=1, max_length=262_144)


__all__ = [
    "NOTEBOOK_EXPORT_MIME_TYPE",
    "NOTEBOOK_MEDIA_TYPE",
    "NotebookCell",
    "NotebookDocument",
    "NotebookDisplayDataOutput",
    "NotebookErrorOutput",
    "NotebookExecuteInput",
    "NotebookExecuteResultOutput",
    "NotebookExportRepresentation",
    "NotebookInputReply",
    "NotebookInputRequest",
    "NotebookOutput",
    "NotebookStreamOutput",
]
