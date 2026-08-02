"""Architecture gate for the R2.32 Runtime checkpoint truth chain."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_managed_runtime_drivers_restore_only_through_typed_registry() -> None:
    for kind in ("terminal", "browser", "canvas", "kernel"):
        source = (ROOT / f"runtime/interactive/{kind}/driver.py").read_text(encoding="utf-8")
        assert "CHECKPOINT_CODEC.decode(checkpoint)" in source
        assert "decode_inline_json" not in source
        assert "checkpoint.codec ==" not in source
        assert "checkpoint.schema_version" not in source


def test_checkpoint_identity_and_evolution_have_one_shared_validator() -> None:
    model = (ROOT / "contracts/runtime/models.py").read_text(encoding="utf-8")
    events = (ROOT / "runtime/session/events.py").read_text(encoding="utf-8")
    projection = (ROOT / "runtime/session/projection.py").read_text(encoding="utf-8")
    host = (ROOT / "runtime/interactive/host.py").read_text(encoding="utf-8")
    assert "def validate_checkpoint_successor" in model
    assert "def _decode_runtime_checkpoint" in events
    checkpoint_decoder = events.split("def _decode_runtime_checkpoint", 1)[1].split("#: Bump when", 1)[0]
    assert "int(payload" not in checkpoint_decoder
    assert "str(payload" not in checkpoint_decoder
    assert "validate_checkpoint_successor" in projection
    assert "validate_checkpoint_successor" in host


def test_codec_registry_has_only_current_versions() -> None:
    codec = (ROOT / "runtime/interactive/checkpoint_codec.py").read_text(encoding="utf-8")
    for owner in (
        "TERMINAL_CHECKPOINT_CODEC",
        "BROWSER_CHECKPOINT_CODEC",
        "CANVAS_CHECKPOINT_CODEC",
        "KERNEL_CHECKPOINT_CODEC",
    ):
        assert owner in codec
    assert "legacy_decoders" not in codec
    assert "checkpoint kind does not match codec owner" in codec
