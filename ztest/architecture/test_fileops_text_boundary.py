from __future__ import annotations

from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def test_text_source_is_the_only_read_search_materialization_entry() -> None:
    materializer = (PACKAGE_ROOT / "runtime/fileops/text_sources.py").read_text(encoding="utf-8")
    assert "from mote.runtime.fileops.encoding import decode_text" in materializer
    assert "extract_document_bytes" in materializer
    assert "is_document" in materializer
    assert "ManagedSnapshotCapture" in materializer

    for relative in (
        "runtime/fileops/search.py",
        "runtime/fileops/text_views.py",
    ):
        source = (PACKAGE_ROOT / relative).read_text(encoding="utf-8")
        assert "from mote.runtime.fileops.encoding import decode_text" not in source
        assert "extract_document_bytes" not in source
        assert "is_document" not in source
        assert "ManagedSnapshotCapture" not in source
        assert "TextSourceService" in source
        assert "self.sources.materialize(" in source


def test_document_registry_does_not_wrap_the_shared_line_model() -> None:
    source = (PACKAGE_ROOT / "runtime/fileops/documents.py").read_text(encoding="utf-8")
    assert "def document_lines(" not in source
