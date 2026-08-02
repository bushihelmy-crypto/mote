"""R3.4 gates for completed migrations with no compatibility residue."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_parallel_kernel_tool_catalog_and_config_watcher_are_deleted() -> None:
    retired = (
        "kernel/tools/__init__.py",
        "kernel/tools/catalog.py",
        "kernel/tools/definitions.py",
        "product/config/watcher.py",
    )
    assert [relative for relative in retired if (ROOT / relative).exists()] == []


def test_obsolete_media_flag_is_deleted() -> None:
    fields = (ROOT / "contracts" / "conversation" / "fields.py").read_text(encoding="utf-8")
    assert "USE_ENCODED_MEDIA" not in fields
    assert "use_encoded_images" not in fields


def test_file_package_does_not_reexport_content_identity() -> None:
    path = ROOT / "contracts" / "file" / "__init__.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assert not any(
        isinstance(node, ast.ImportFrom) and any(alias.name == "ContentIdentity" for alias in node.names)
        for node in ast.walk(tree)
    )
    assert '"ContentIdentity"' not in path.read_text(encoding="utf-8")


def test_code_map_model_has_no_false_extractor_reexport_claim() -> None:
    model = (ROOT / "runtime" / "code_map" / "model.py").read_text(encoding="utf-8")
    assert "backwards compatibility" not in model
    assert "re-exports every name" not in model


def test_contracts_and_kernel_docs_do_not_name_deleted_common_owner() -> None:
    violations: list[str] = []
    for package in ("contracts", "kernel"):
        for path in (ROOT / package).rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            if "common/" in source or "common." in source:
                violations.append(path.relative_to(ROOT).as_posix())
    assert violations == []
