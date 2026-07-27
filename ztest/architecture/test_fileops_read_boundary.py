from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def test_managed_observations_share_one_capture_path() -> None:
    capture = (PACKAGE_ROOT / "runtime/fileops/capture.py").read_text(encoding="utf-8")
    assert "capture_lease" in capture
    for relative in (
        "runtime/fileops/byte_views.py",
        "runtime/fileops/pdf_views.py",
        "runtime/fileops/text_sources.py",
        "runtime/fileops/facade.py",
    ):
        source = (PACKAGE_ROOT / relative).read_text(encoding="utf-8")
        assert "capture_lease" not in source
        assert "ManagedSnapshotCapture" in source or relative.endswith("facade.py")

    for relative in (
        "runtime/fileops/search.py",
        "runtime/fileops/text_views.py",
    ):
        source = (PACKAGE_ROOT / relative).read_text(encoding="utf-8")
        assert "capture_lease" not in source
        assert "ManagedSnapshotCapture" not in source
        assert "TextSourceService" in source


def test_read_product_only_adapts_managed_read_views() -> None:
    source = (PACKAGE_ROOT / "product/toolsets/builtin/read.py").read_text(encoding="utf-8")
    assert '"read_file_view"' in source
    assert '"read_file_byte_view"' not in source
    assert '"read_pdf_view"' not in source
    assert '"read_file_text_view"' not in source
    assert 'mode in ("raw", "hex")' in source
    assert 'mode == "render"' in source
    for request in (
        "ByteReadRequest",
        "ContinueReadRequest",
        "PdfReadRequest",
        "TextReadRequest",
    ):
        assert request in source


def test_read_dispatch_uses_a_closed_typed_request_union() -> None:
    models = (PACKAGE_ROOT / "contracts/fileops/models.py").read_text(encoding="utf-8")
    facade = (PACKAGE_ROOT / "runtime/fileops/facade.py").read_text(encoding="utf-8")
    assert "ReadRequest = Union[" in models
    assert "class ContinueReadRequest" in models
    assert "cursor: str" in models
    assert "request: ReadRequest" in facade
    assert "**kwargs" not in facade[facade.index("    def read_view(") : facade.index("    def search(")]


def test_all_read_views_share_one_tagged_cursor_store() -> None:
    cursor_source = (PACKAGE_ROOT / "runtime/fileops/read_cursors.py").read_text(encoding="utf-8")
    assert "class ReadCursorStore" in cursor_source
    assert '"kind": kind.value' in cursor_source
    assert "ReadCursorKind" in cursor_source

    for relative in (
        "runtime/fileops/byte_views.py",
        "runtime/fileops/pdf_views.py",
        "runtime/fileops/text_views.py",
    ):
        source = (PACKAGE_ROOT / relative).read_text(encoding="utf-8")
        assert "ReadCursorStore" in source
        assert "import base64" not in source
        assert "import json" not in source
        assert "def _encode_cursor" not in source
        assert "def _decode_cursor" not in source

    facade = (PACKAGE_ROOT / "runtime/fileops/facade.py").read_text(encoding="utf-8")
    assert facade.count("ReadCursorStore(") == 1
    assert facade.count("cursors=self.read_cursors") == 3


def test_read_and_search_share_only_the_durable_cursor_registry() -> None:
    assert not (PACKAGE_ROOT / "runtime/fileops/artifact_cursors.py").exists()
    registry = (PACKAGE_ROOT / "runtime/fileops/cursor_registry.py").read_text(encoding="utf-8")
    facade = (PACKAGE_ROOT / "runtime/fileops/facade.py").read_text(encoding="utf-8")
    search = (PACKAGE_ROOT / "runtime/fileops/search.py").read_text(encoding="utf-8")
    read = (PACKAGE_ROOT / "runtime/fileops/read_cursors.py").read_text(encoding="utf-8")

    assert "class DurableCursorRegistry" in registry
    assert "root_digest" in registry
    assert "position" in registry
    assert "hmac.digest" in registry
    assert facade.count("DurableCursorRegistry(") == 1
    assert "DurableCursorRegistry" in search
    assert "DurableCursorRegistry" in read
    assert "ArtifactCursorCodec" not in search + read


def test_product_read_exposes_exactly_one_cursor_parameter() -> None:
    source = (PACKAGE_ROOT / "product/toolsets/builtin/read.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    read_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Read")
    call = next(node for node in read_class.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "call")
    parameters = [argument.arg for argument in (call.args.posonlyargs + call.args.args + call.args.kwonlyargs)]
    cursor_parameters = [name for name in parameters if "cursor" in name]

    assert cursor_parameters == ["cursor"]
    assert "next_offset to continue" not in source


def test_read_product_has_no_text_decode_or_document_extraction_bypass() -> None:
    source = (PACKAGE_ROOT / "product/toolsets/builtin/read.py").read_text(encoding="utf-8")
    for bypass in (
        "decode_text(",
        "document_lines(",
        "extract_document_bytes(",
        "is_document(",
        "def _read_text(",
        "def _read_document(",
        "def _read_range(",
    ):
        assert bypass not in source
