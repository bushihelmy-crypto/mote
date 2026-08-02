"""Architecture gate for the R2.27 verified identity/install commit chain."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_session_read_and_projection_reassert_identity_invariants() -> None:
    log = (ROOT / "runtime/session/log.py").read_text(encoding="utf-8")
    projection = (ROOT / "runtime/session/projection.py").read_text(encoding="utf-8")
    assert "_iter_identity_verified" in log
    assert "Session metadata must be the unique first fact" in log
    assert "Session envelope identity does not match its stream" in log
    assert "Session metadata identity does not match its stream" in log
    assert "SessionProjectionIdentityError" in projection
    assert "Session metadata must be the unique first fact" in projection
    assert "Session metadata, envelope, and stream identities differ" in projection


def test_residency_install_is_revisioned_fenced_and_rollback_safe() -> None:
    model = (ROOT / "orchestration/agents/residency/model.py").read_text(encoding="utf-8")
    store = (ROOT / "orchestration/agents/residency/store.py").read_text(encoding="utf-8")
    control = (ROOT / "orchestration/agents/control.py").read_text(encoding="utf-8")
    assert "INSTALLING" in model and "install_fence" in model
    assert "Residency forget requires an install claim" in store
    assert "expected_record_revision" in store
    assert "Residency forget install owner mismatch" in store
    assert "rollback_generation" in control
    assert "self._remove_runtime(agent_id)" in control
    assert "slot.rollback()" in control
