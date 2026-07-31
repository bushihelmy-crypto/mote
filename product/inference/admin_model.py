"""Typed data dependencies for inference administration projections."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class AdminReadModel:
    providers: Callable[[], Awaitable[Sequence[Mapping[str, Any]]]]
    credentials: Callable[[], Awaitable[Sequence[Mapping[str, Any]]]]
    generations: Callable[[], Awaitable[Sequence[Mapping[str, Any]]]]
    readiness: Callable[[], Awaitable[Mapping[str, Any]]]
    receipt: Callable[[str], Awaitable[Any | None]]
    reconciliation: Callable[[], Awaitable[Sequence[Mapping[str, Any]]]]
    audit: Callable[[int], Awaitable[Sequence[Mapping[str, Any]]]]


__all__ = ["AdminReadModel"]
