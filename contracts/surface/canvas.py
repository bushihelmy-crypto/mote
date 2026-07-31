"""Stable vector-canvas document and operation contracts."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field, JsonValue, model_validator

_COLOR_PATTERN = r"^(?:#[0-9a-fA-F]{3,8}|[a-zA-Z]+|none)$"


@dataclass(frozen=True, slots=True)
class CanvasExportRepresentation:
    """One immutable Canvas export before Artifact publication policy is applied."""

    representation: str
    mime_type: str
    content: bytes = field(repr=False)
    suggested_name: str = ""


class CanvasStyle(BaseModel):
    fill: str = Field(default="none", pattern=_COLOR_PATTERN)
    stroke: str = Field(default="#7aa2f7", pattern=_COLOR_PATTERN)
    stroke_width: float = Field(default=2.0, ge=0.0, le=64.0)
    font_size: float = Field(default=24.0, ge=4.0, le=256.0)


class CanvasElement(BaseModel):
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    kind: Literal["rect", "ellipse", "line", "arrow", "text"]
    x: float = 0.0
    y: float = 0.0
    width: float = Field(default=0.0, ge=0.0)
    height: float = Field(default=0.0, ge=0.0)
    x2: float = 0.0
    y2: float = 0.0
    source_id: str = Field(default="", max_length=128, pattern=r"^(?:[A-Za-z0-9_.:-]+)?$")
    target_id: str = Field(default="", max_length=128, pattern=r"^(?:[A-Za-z0-9_.:-]+)?$")
    text: str = Field(default="", max_length=10_000)
    style: CanvasStyle = Field(default_factory=CanvasStyle)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    extensions: dict[str, JsonValue] = Field(default_factory=dict)


class CanvasOperation(BaseModel):
    op: Literal["upsert", "remove", "clear"]
    element: CanvasElement | None = None
    element_id: str = ""

    @model_validator(mode="after")
    def _validate_payload(self) -> "CanvasOperation":
        if self.op == "upsert" and self.element is None:
            raise ValueError("upsert requires element")
        if self.op == "remove" and not self.element_id:
            raise ValueError("remove requires element_id")
        return self


class CanvasDocument(BaseModel):
    width: int = Field(default=1200, ge=64, le=16_384)
    height: int = Field(default=800, ge=64, le=16_384)
    background: str = Field(default="#ffffff", pattern=_COLOR_PATTERN)
    elements: list[CanvasElement] = Field(default_factory=list)
    extensions: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _unique_ids(self) -> "CanvasDocument":
        ids = [element.id for element in self.elements]
        if len(ids) != len(set(ids)):
            raise ValueError("canvas element ids must be unique")
        return self


__all__ = [
    "CanvasDocument",
    "CanvasElement",
    "CanvasExportRepresentation",
    "CanvasOperation",
    "CanvasStyle",
]
