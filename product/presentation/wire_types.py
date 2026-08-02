"""Shared mutable JSON shapes used by Product presentation protocols."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TypeAlias, cast

WireJsonScalar: TypeAlias = None | bool | int | float | str
WireJsonValue: TypeAlias = WireJsonScalar | list["WireJsonValue"] | dict[str, "WireJsonValue"]
WireObject: TypeAlias = dict[str, WireJsonValue]
WireMapping: TypeAlias = Mapping[str, WireJsonValue]


def to_wire_json(value: object) -> WireJsonValue:
    if value is None or type(value) in {bool, int, float, str}:
        return cast(WireJsonScalar, value)
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("wire JSON object keys must be strings")
        return {str(key): to_wire_json(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [to_wire_json(item) for item in value]
    raise TypeError(f"value is not JSON-safe: {type(value).__name__}")


__all__ = ["WireJsonScalar", "WireJsonValue", "WireMapping", "WireObject", "to_wire_json"]
