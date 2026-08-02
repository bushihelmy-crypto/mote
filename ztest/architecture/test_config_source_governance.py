"""Static gates for canonical Product configuration source identity."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_config_source_root_promotion_path_is_deleted() -> None:
    violations: list[str] = []
    for path in (ROOT / "product").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "source_root" in source or "ConfigSource.PROJECT" in source:
            violations.append(path.relative_to(ROOT).as_posix())
    assert not violations


def test_source_descriptor_carries_canonical_identity_and_trust() -> None:
    source = (ROOT / "product/config/sources.py").read_text(encoding="utf-8")
    for field in ("canonical_path: Path", "device: int", "inode: int", "trusted: bool"):
        assert field in source
