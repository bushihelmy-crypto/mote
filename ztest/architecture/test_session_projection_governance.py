from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_session_projections_enter_through_verified_session_log() -> None:
    assert ".iter_events()" in _source("runtime/session/checkpoint.py")
    assert "log.iter_events()" in _source("runtime/session/history.py")
    assert "log.iter_events()" in _source("runtime/session/artifact_roots.py")
    assert "session_log.iter_events()" in _source("runtime/agent/role_components.py")
    assert "SessionLog" in _source("runtime/session/runtime_projection.py")


def test_lite_listing_verifies_activation_before_raw_window_projection() -> None:
    source = _source("runtime/session/listing.py")
    verification = source.index("SessionLog(session_id")
    raw_projection = source.index("rollout.stat()", verification)
    assert verification < raw_projection


def test_legacy_decoder_is_offline_only_and_has_no_composition_path() -> None:
    production = tuple((ROOT / "product").rglob("*.py"))
    imports = [path for path in production if "migrations.session_stream" in path.read_text(encoding="utf-8")]
    assert not imports
    runtime_sources = {
        path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8") for path in (ROOT / "runtime").rglob("*.py")
    }
    direct_decoders = {path for path, source in runtime_sources.items() if "decode_event_record" in source}
    assert direct_decoders & {
        "runtime/session/log.py",
        "runtime/session/listing.py",
    } == {
        "runtime/session/log.py",
        "runtime/session/listing.py",
    }
    assert not any("migrate_session_stream_v1" in source for source in runtime_sources.values())
