"""Ambient identity of the tool call currently crossing the executor."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator, Mapping

from mote.contracts.tool.identity import ToolInvocationIdentity

_tool_call_id: ContextVar[str | None] = ContextVar("mote_tool_call_id", default=None)
_authorized_invocation: ContextVar["AuthorizedToolInvocation | None"] = ContextVar(
    "mote_authorized_tool_invocation", default=None
)


@dataclass(frozen=True, slots=True)
class AuthorizedToolInvocation:
    identity: ToolInvocationIdentity
    tool_name: str
    arguments: Mapping[str, Any]
    generation: int


def current_tool_call_id() -> str | None:
    return _tool_call_id.get()


@contextmanager
def bind_tool_call_id(call_id: str | None) -> Iterator[None]:
    token = _tool_call_id.set(call_id)
    try:
        yield
    finally:
        _tool_call_id.reset(token)


def current_authorized_invocation() -> AuthorizedToolInvocation | None:
    return _authorized_invocation.get()


@contextmanager
def bind_authorized_invocation(invocation: AuthorizedToolInvocation) -> Iterator[None]:
    token = _authorized_invocation.set(invocation)
    try:
        yield
    finally:
        _authorized_invocation.reset(token)
