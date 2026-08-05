from types import SimpleNamespace

from mote.runtime.session.artifact_roots import SessionFileOpsArtifactRoots


def test_legacy_session_without_activation_manifest_is_not_scanned(tmp_path):
    legacy = tmp_path / "legacy-session"
    legacy.mkdir()
    (legacy / "rollout.jsonl").write_text("legacy data", encoding="utf-8")

    roots = SessionFileOpsArtifactRoots(tmp_path, SimpleNamespace())

    assert roots.artifact_roots() == ()


def test_excluded_live_session_is_not_scanned(tmp_path):
    live = tmp_path / "live-session"
    live.mkdir()
    (live / "rollout.jsonl").write_text("active", encoding="utf-8")
    (live / "stream-manifest.json").write_text("{}", encoding="utf-8")

    roots = SessionFileOpsArtifactRoots(
        tmp_path,
        SimpleNamespace(),
        excluded_session_ids=frozenset({"live-session"}),
    )

    assert roots.artifact_roots() == ()
