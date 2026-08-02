"""Product composition adapter for the Code Map runtime."""

from pathlib import Path

from mote.contracts.ports.code_intelligence.code_map import CodeMapTurnSourceRequest
from mote.product.code_map.paths import codemap_db_path
from mote.product.code_map.turn_context import CodeMapContextSource
from mote.runtime.code_map.indexer import RepoIndexer
from mote.runtime.code_map.languages import registered_extensions

DEFAULT_EXCLUDED_DIRECTORIES = {
    ".git",
    ".mote",
    ".cache",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    "target",
    "bin",
    "obj",
    "dist",
    "build",
    "out",
    ".gradle",
    ".idea",
    "vendor",
    "Pods",
    "DerivedData",
    "cmake-build-debug",
}


class ProductCodeMapIndexerFactory:
    def __init__(
        self,
        enabled_extensions: set[str] | None = None,
        *,
        codemap_root: Path,
    ) -> None:
        self._enabled_extensions = enabled_extensions
        self._codemap_root = codemap_root

    def build(self, repo_root: str) -> RepoIndexer:
        extensions = self._enabled_extensions or registered_extensions()
        return RepoIndexer(
            repo_root,
            store_path=codemap_db_path(repo_root, codemap_root=self._codemap_root),
            enabled_extensions=set(extensions),
            excluded_directories=set(DEFAULT_EXCLUDED_DIRECTORIES),
        )

    def build_turn_source(self, request: CodeMapTurnSourceRequest) -> CodeMapContextSource:
        return CodeMapContextSource(
            get_touched_files=request.get_touched_files,
            lsp_query=request.lsp_query,
            repo_index=request.repo_index,
            get_read_state=request.get_read_state,
            get_glimpsed_files=request.get_glimpsed_files,
            surface_callers=request.surface_callers,
        )


__all__ = ["DEFAULT_EXCLUDED_DIRECTORIES", "ProductCodeMapIndexerFactory"]
