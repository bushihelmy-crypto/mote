from __future__ import annotations

from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def test_search_has_one_matching_engine_and_one_document_registry() -> None:
    assert not (PACKAGE_ROOT / "product/toolsets/builtin/_search_engine.py").exists()
    assert not (PACKAGE_ROOT / "product/toolsets/builtin/assets/ripgrep/README.md").exists()
    assert not (PACKAGE_ROOT / "runtime/tools/dependency/_document.py").exists()
    assert not tuple((PACKAGE_ROOT / "runtime/tools/dependency/document_adapters").glob("*.py"))

    product = (PACKAGE_ROOT / "product/toolsets/builtin/search.py").read_text(encoding="utf-8")
    assert "subprocess" not in product
    assert "extract_document" not in product
    assert "re.compile" not in product
    assert 'errors="replace"' not in product
    assert 'split(":"' not in product
    assert 'requires = ("get_cwd", "search_files", "record_file_glimpsed")' in product
    assert (PACKAGE_ROOT / "runtime/fileops/assets/ripgrep/README.md").is_file()
    assert (PACKAGE_ROOT / "runtime/fileops/assets/ripgrep/x86_64-linux/rg").is_file()


def test_optional_document_adapters_are_declared_at_module_scope() -> None:
    registry = (PACKAGE_ROOT / "runtime/fileops/documents.py").read_text(encoding="utf-8")
    assert "import_module" not in registry
    assert "mote.runtime.fileops.document_adapters" in registry
