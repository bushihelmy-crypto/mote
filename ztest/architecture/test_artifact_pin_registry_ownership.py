from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_all_layout_collectors_consume_canonical_pin_registry() -> None:
    layout = (ROOT / "runtime/artifacts/layout.py").read_text(encoding="utf-8")
    assert "pins = ArtifactPinRegistry()" in layout
    assert "pin_sources=(pins,)" in layout
    session = (ROOT / "runtime/agent/components/session.py").read_text(encoding="utf-8")
    assert "bundle.pins.register_source(" in session
    assert "fileops-cursors:" in session


def test_collector_never_scans_producer_private_state() -> None:
    collector = (ROOT / "runtime/artifacts/gc.py").read_text(encoding="utf-8")
    assert "freeze_artifact_pins" in collector
    for forbidden in ("cursor_registry", "self._pins", "self._cursors", "self._tasks"):
        assert forbidden not in collector


def test_every_production_cas_writer_has_one_canonical_reachability_owner() -> None:
    production = tuple(
        path for package in ("runtime", "orchestration", "product") for path in (ROOT / package).rglob("*.py")
    )
    constructors = {
        path.relative_to(ROOT).as_posix()
        for path in production
        if "ContentAddressedArtifactStore(" in path.read_text(encoding="utf-8")
    }
    assert constructors == {
        "runtime/artifacts/layout.py",
        "runtime/fileops/transactions.py",
    }
    store = (ROOT / "runtime/artifacts/store.py").read_text(encoding="utf-8")
    assert "WHERE released = 0" in store
    assert "artifact_publication_outbox_representations" in store
    assert "WHERE outbox.state IN ('queued', 'failed')" in store
    cleanup = (ROOT / "runtime/session/workspace/cleanup.py").read_text(encoding="utf-8")
    assert "session_id in legal_hold_session_ids" in cleanup
    assert cleanup.index("release_session_scope()") < cleanup.index("collector.collect()")
