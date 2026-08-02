"""Typed query and Product construction ports for repository Code Map."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol

from mote.contracts.events.envelope import JsonValue
from mote.contracts.file.identity import PresentVersion
from mote.contracts.ports.conversation.turn_context import EphemeralContextSource


@dataclass(frozen=True, slots=True)
class CodeSymbol:
    name: str
    qualified_name: str
    kind: str
    start_line: int
    signature: str = ""
    summary: str = ""

    def __post_init__(self) -> None:
        for name in ("name", "qualified_name", "kind"):
            value = getattr(self, name)
            if type(value) is not str or not value:
                raise ValueError(f"code symbol {name} must be a non-empty string")
        if type(self.start_line) is not int or self.start_line <= 0:
            raise ValueError("code symbol start_line must be a positive integer")
        for name in ("signature", "summary"):
            if type(getattr(self, name)) is not str:
                raise ValueError(f"code symbol {name} must be a string")


@dataclass(frozen=True, slots=True)
class CodeReference:
    path: str
    line: int

    def __post_init__(self) -> None:
        if type(self.path) is not str or not self.path:
            raise ValueError("code reference path must be a non-empty string")
        if type(self.line) is not int or self.line <= 0:
            raise ValueError("code reference line must be a positive integer")


class CodeMapQueryPort(Protocol):
    def symbols_in(self, path: str) -> tuple[CodeSymbol, ...]: ...

    def module_summary_of(self, path: str) -> str | None: ...

    def importers(self, candidates: Iterable[str]) -> tuple[str, ...]: ...

    def references_to(self, path: str, symbol: str) -> tuple[CodeReference, ...]: ...


class CodeMapLspQueryPort(Protocol):
    async def document_symbols(self, path: str) -> list[dict[str, JsonValue]]: ...

    async def definition(self, path: str, line: int, character: int) -> list[dict[str, JsonValue]]: ...

    async def references(self, path: str, line: int, character: int) -> list[dict[str, JsonValue]]: ...


@dataclass(frozen=True, slots=True)
class CodeMapTurnSourceRequest:
    get_touched_files: Callable[[], list[str]]
    repo_index: CodeMapQueryPort | None
    get_read_state: Callable[[], Mapping[str, PresentVersion]]
    get_glimpsed_files: Callable[[], list[str]]
    lsp_query: CodeMapLspQueryPort | None = None
    surface_callers: bool = False


class CodeMapIndexer(CodeMapQueryPort, Protocol):
    async def scan_all_async(self) -> None: ...

    async def refresh_async(self, paths: list[str]) -> None: ...

    def close(self) -> None: ...


class CodeMapIndexerFactory(Protocol):
    def build(self, repo_root: str) -> CodeMapIndexer: ...

    def build_turn_source(
        self,
        request: CodeMapTurnSourceRequest,
    ) -> EphemeralContextSource: ...


__all__ = [
    "CodeMapIndexer",
    "CodeMapIndexerFactory",
    "CodeMapLspQueryPort",
    "CodeMapQueryPort",
    "CodeMapTurnSourceRequest",
    "CodeReference",
    "CodeSymbol",
]
