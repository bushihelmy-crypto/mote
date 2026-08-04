"""Canonical execution-lineage declaration and strict wire codec."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

SCOPE_PATH_SCHEMA = "mote.execution-scope/v1"
MAX_SCOPE_DEPTH = 64
MAX_SCOPE_TEXT_BYTES = 256


@dataclass(frozen=True, slots=True)
class ScopeRef:
    kind: str
    id: str
    label: str

    def __post_init__(self) -> None:
        for name in ("kind", "id", "label"):
            value = getattr(self, name)
            if type(value) is not str or not value:
                raise ValueError(f"ScopeRef.{name} must be a non-empty string")
            if len(value.encode("utf-8")) > MAX_SCOPE_TEXT_BYTES:
                raise ValueError(f"ScopeRef.{name} exceeds its byte bound")


ScopePath: TypeAlias = tuple[ScopeRef, ...]


def encode_scope_path(scope: ScopePath) -> dict[str, object]:
    if len(scope) > MAX_SCOPE_DEPTH:
        raise ValueError("execution scope exceeds its depth bound")
    return {
        "schema": SCOPE_PATH_SCHEMA,
        "path": [{"kind": ref.kind, "id": ref.id, "label": ref.label} for ref in scope],
    }


def decode_scope_path(payload: object) -> ScopePath:
    if type(payload) is not dict or set(payload) != {"schema", "path"}:
        raise ValueError("execution scope envelope has unsupported fields")
    if payload["schema"] != SCOPE_PATH_SCHEMA:
        raise ValueError("execution scope schema is unsupported")
    path = payload["path"]
    if type(path) is not list or len(path) > MAX_SCOPE_DEPTH:
        raise ValueError("execution scope path is invalid")
    refs: list[ScopeRef] = []
    for item in path:
        if type(item) is not dict or set(item) != {"kind", "id", "label"}:
            raise ValueError("execution scope reference has unsupported fields")
        kind, identity, label = item["kind"], item["id"], item["label"]
        if any(type(value) is not str for value in (kind, identity, label)):
            raise TypeError("execution scope reference fields must be strings")
        refs.append(ScopeRef(kind, identity, label))
    return tuple(refs)


__all__ = [
    "MAX_SCOPE_DEPTH",
    "SCOPE_PATH_SCHEMA",
    "ScopePath",
    "ScopeRef",
    "decode_scope_path",
    "encode_scope_path",
]
