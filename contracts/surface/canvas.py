"""Stable vector-canvas document and operation contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

_COLOR_PATTERN = r"^(?:#[0-9a-fA-F]{3,8}|[a-zA-Z]+|none)$"


class _StrictCanvasModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True, slots=True)
class CanvasExportRepresentation:
    """One immutable Canvas export before Artifact publication policy is applied."""

    representation: str
    mime_type: str
    content: bytes = field(repr=False)
    suggested_name: str = ""


class CanvasStyle(_StrictCanvasModel):
    fill: str = Field(default="none", pattern=_COLOR_PATTERN)
    stroke: str = Field(default="#7aa2f7", pattern=_COLOR_PATTERN)
    stroke_width: float = Field(default=2.0, ge=0.0, le=64.0)
    font_size: float = Field(default=24.0, ge=4.0, le=256.0)


class _CanvasElementFields(_StrictCanvasModel):
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
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


class CanvasRectangle(_CanvasElementFields):
    kind: Literal["rect"] = "rect"


class CanvasEllipse(_CanvasElementFields):
    kind: Literal["ellipse"] = "ellipse"


class CanvasLine(_CanvasElementFields):
    kind: Literal["line"] = "line"


class CanvasArrow(_CanvasElementFields):
    kind: Literal["arrow"] = "arrow"


class CanvasText(_CanvasElementFields):
    kind: Literal["text"] = "text"


CanvasElement: TypeAlias = Annotated[
    CanvasRectangle | CanvasEllipse | CanvasLine | CanvasArrow | CanvasText,
    Field(discriminator="kind"),
]


class CanvasOperation(_StrictCanvasModel):
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


class CanvasDocument(_StrictCanvasModel):
    schema_version: Literal["1"] = "1"
    revision: int = Field(default=0, ge=0)
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
    "CanvasArrow",
    "CanvasElement",
    "CanvasEllipse",
    "CanvasExportRepresentation",
    "CanvasOperation",
    "CanvasLine",
    "CanvasRectangle",
    "CanvasStyle",
    "CanvasText",
]
