"""Ambient identity of the tool call currently crossing the executor."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator

from mote.contracts.tool.arguments import ToolArguments, freeze_tool_arguments
from mote.contracts.tool.identity import ToolInvocationIdentity

_tool_call_id: ContextVar[str | None] = ContextVar("mote_tool_call_id", default=None)
_authorized_invocation: ContextVar["AuthorizedToolInvocation | None"] = ContextVar(
    "mote_authorized_tool_invocation", default=None
)
_fileops_transaction_id: ContextVar[str | None] = ContextVar("mote_fileops_transaction_id", default=None)


@dataclass(frozen=True, slots=True)
class AuthorizedToolInvocation:
    identity: ToolInvocationIdentity
    tool_name: str
    arguments: ToolArguments
    generation: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", freeze_tool_arguments(self.arguments))


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


def current_fileops_transaction_id() -> str | None:
    return _fileops_transaction_id.get()


@contextmanager
def bind_authorized_invocation(invocation: AuthorizedToolInvocation) -> Iterator[None]:
    token = _authorized_invocation.set(invocation)
    try:
        yield
    finally:
        _authorized_invocation.reset(token)


@contextmanager
def bind_fileops_transaction_id(transaction_id: str | None) -> Iterator[None]:
    token = _fileops_transaction_id.set(transaction_id)
    try:
        yield
    finally:
        _fileops_transaction_id.reset(token)
