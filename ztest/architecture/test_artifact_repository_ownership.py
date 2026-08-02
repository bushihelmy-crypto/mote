from __future__ import annotations

import ast
from pathlib import Path

from mote.runtime.artifacts.repository import ContentAddressedArtifactStore
from mote.runtime.fileops.mutation.artifacts import FileMutationArtifactRepository

ROOT = Path(__file__).resolve().parents[2]


def _production_files() -> tuple[Path, ...]:
    return tuple(
        path
        for package in ("contracts", "kernel", "runtime", "orchestration", "product")
        for path in (ROOT / package).rglob("*.py")
    )


def test_artifact_implementations_have_distinct_canonical_names() -> None:
    assert ContentAddressedArtifactStore.__name__ == "ContentAddressedArtifactStore"
    assert FileMutationArtifactRepository.__name__ == "FileMutationArtifactRepository"
    assert ContentAddressedArtifactStore is not FileMutationArtifactRepository


def test_old_ambiguous_repository_type_has_no_production_definition_or_import() -> None:
    violations: list[str] = []
    for path in _production_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "ArtifactRepository"
            ):
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}: definition")
            if isinstance(node, ast.ImportFrom):
                for imported in node.names:
                    if imported.name == "ArtifactRepository":
                        violations.append(f"{path.relative_to(ROOT)}:{node.lineno}: import")
    assert violations == []


def test_file_mutation_repository_requires_the_canonical_content_store(tmp_path: Path) -> None:
    content = ContentAddressedArtifactStore(tmp_path / "content", hard_limit_bytes=1024)
    mutation = FileMutationArtifactRepository(
        content,
        lifecycle_root=tmp_path / "lifecycle",
        hard_limit_bytes=1024,
    )
    assert mutation.content_repository is content
    assert mutation.catalog.control_root.parent == tmp_path / "lifecycle"
