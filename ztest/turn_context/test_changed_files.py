"""Tests for the typed external-change turn-context feed."""

from __future__ import annotations

import asyncio

from mote.contracts.events.file.observation import FileChangedEvent, FileMutatedEvent
from mote.contracts.file.identity import AbsentVersion, FileChangeKind, NameIdentity, PresentVersion, TargetIdentity
from mote.contracts.ports.conversation.turn_context import EphemeralContextSource
from mote.runtime.context.turn import ChangedFilesContextSource


def run(coro):
    return asyncio.run(coro)


def _present(seed: str = "a") -> PresentVersion:
    return PresentVersion(
        name_identity=NameIdentity("name", "test-name"),
        target_identity=TargetIdentity(f"target-{seed}", "test-target"),
        size=1,
        mtime_ns=1,
        digest=seed * 64,
        metadata_digest="f" * 64,
    )


def _event(path: str, kind: FileChangeKind = FileChangeKind.MODIFIED) -> FileChangedEvent:
    prior = _present("a")
    if kind is FileChangeKind.CREATED:
        return FileChangedEvent(path, kind, AbsentVersion(prior.name_identity), prior)
    if kind is FileChangeKind.DELETED:
        return FileChangedEvent(path, kind, prior, AbsentVersion(prior.name_identity))
    return FileChangedEvent(path, kind, prior, _present("b"))


def test_is_ephemeral_context_source_and_telemetry_handler():
    source = ChangedFilesContextSource()
    assert isinstance(source, EphemeralContextSource)
    assert callable(getattr(source, "handle", None))


def test_empty_state_returns_none():
    assert run(ChangedFilesContextSource().render()) is None


def test_external_change_reported_once(tmp_path):
    source = ChangedFilesContextSource()
    path = str(tmp_path / "a.py")
    run(source.handle(_event(path)))
    out = run(source.render(cwd=str(tmp_path)))
    assert out is not None
    assert "Files changed on disk" in out
    assert "a.py" in out
    assert run(source.render(cwd=str(tmp_path))) is None


def test_latest_transition_per_path_wins(tmp_path):
    source = ChangedFilesContextSource()
    path = str(tmp_path / "a.py")
    run(source.handle(_event(path)))
    run(source.handle(_event(path, FileChangeKind.DELETED)))
    out = run(source.render(cwd=str(tmp_path)))
    assert out is not None
    assert "a.py (deleted)" in out


def test_self_mutation_is_not_external_attribution(tmp_path):
    source = ChangedFilesContextSource()
    run(source.handle(FileMutatedEvent(path=str(tmp_path / "a.py"), tool="Write")))
    assert run(source.render(cwd=str(tmp_path))) is None


def test_relative_display_uses_cwd(tmp_path):
    source = ChangedFilesContextSource()
    path = str(tmp_path / "pkg" / "a.py")
    run(source.handle(_event(path)))
    out = run(source.render(cwd=str(tmp_path)))
    assert out is not None
    assert "pkg/a.py" in out
