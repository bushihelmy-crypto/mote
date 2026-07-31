"""Construction port for the Product-configured Code Map runtime."""

from __future__ import annotations

from typing import Any, Protocol


class CodeMapIndexer(Protocol):
    async def scan_all_async(self) -> object:
        ...

    def close(self) -> None:
        ...


class CodeMapIndexerFactory(Protocol):
    def build(self, repo_root: str) -> CodeMapIndexer:
        ...

    def build_turn_source(self, **kwargs: Any) -> object:
        ...


__all__ = ["CodeMapIndexer", "CodeMapIndexerFactory"]
