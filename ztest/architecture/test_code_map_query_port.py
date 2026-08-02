from __future__ import annotations

from pathlib import Path

import pytest

from mote.contracts.ports.code_intelligence.code_map import (
    CodeMapQueryPort,
    CodeMapTurnSourceRequest,
    CodeReference,
    CodeSymbol,
)
from mote.product.code_map.factory import ProductCodeMapIndexerFactory
from mote.runtime.code_map.indexer import RepoIndexer


class FakeCodeMapQueries:
    def symbols_in(self, path: str) -> tuple[CodeSymbol, ...]:
        return ()

    def module_summary_of(self, path: str) -> str | None:
        return None

    def importers(self, candidates) -> tuple[str, ...]:
        return ()

    def references_to(self, path: str, symbol: str) -> tuple[CodeReference, ...]:
        return ()


def test_repo_index_query_dtos_are_sorted_and_missing_is_explicit(tmp_path) -> None:
    source = tmp_path / "sample.py"
    source.write_text("def zebra():\n    pass\n\ndef alpha():\n    zebra()\n", encoding="utf-8")
    index = RepoIndexer(
        str(tmp_path),
        store_path=":memory:",
        enabled_extensions={".py"},
        excluded_directories=set(),
    )
    try:
        index.scan_all()
        symbols = index.symbols_in(str(source))
        references = index.references_to(str(source), "zebra")

        assert all(isinstance(symbol, CodeSymbol) for symbol in symbols)
        assert [symbol.name for symbol in symbols] == ["zebra", "alpha"]
        assert all(isinstance(reference, CodeReference) for reference in references)
        assert index.symbols_in(str(tmp_path / "missing.py")) == ()
        assert index.module_summary_of(str(tmp_path / "missing.py")) is None
        assert index.references_to(str(tmp_path / "missing.py"), "none") == ()
    finally:
        index.close()


def test_product_builds_turn_source_from_fake_query_port(tmp_path) -> None:
    queries: CodeMapQueryPort = FakeCodeMapQueries()
    factory = ProductCodeMapIndexerFactory(codemap_root=tmp_path / "maps")

    source = factory.build_turn_source(
        CodeMapTurnSourceRequest(
            get_touched_files=lambda: [],
            repo_index=queries,
            get_read_state=lambda: {},
            get_glimpsed_files=lambda: [],
        )
    )

    assert source.name == "code_map"


def test_factory_and_product_enrichment_have_no_dynamic_query_boundary() -> None:
    factory = Path("product/code_map/factory.py").read_text(encoding="utf-8")
    enrichment = Path("product/code_map/enrichment.py").read_text(encoding="utf-8")
    contract = Path("contracts/ports/code_intelligence/code_map.py").read_text(encoding="utf-8")

    assert "**kwargs" not in factory
    assert "getattr(repo_index" not in enrichment
    assert "-> object" not in contract
    assert "Any" not in contract


@pytest.mark.parametrize(
    "factory",
    (
        lambda: CodeSymbol("", "qualified", "function", 1),
        lambda: CodeSymbol("name", "qualified", "function", True),
        lambda: CodeSymbol("name", "qualified", "function", -1),
        lambda: CodeSymbol("name", "qualified", "function", 1, signature=object()),
        lambda: CodeReference("", 1),
        lambda: CodeReference("path.py", True),
        lambda: CodeReference("path.py", 0),
    ),
)
def test_code_map_dtos_reject_invalid_identity_and_location(factory) -> None:
    with pytest.raises(ValueError):
        factory()
