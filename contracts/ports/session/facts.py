"""Explicit capability for committing recoverable session facts."""

from __future__ import annotations

from typing import Protocol

from mote.contracts.ports.events.journal import AppendResult


class SessionFactSink(Protocol):
    async def commit_fact(self, event: object) -> AppendResult | None:
        ...


__all__ = ["SessionFactSink"]
