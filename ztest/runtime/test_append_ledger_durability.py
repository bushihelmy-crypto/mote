from __future__ import annotations

import errno

import pytest

from mote.runtime.ledger import (
    KIND_TOOL,
    STARTED,
    LedgerCorruptionError,
    LedgerPersistenceError,
    RunJournal,
    RunJournalLifecycleError,
    run_journaled_step,
)
from mote.runtime.ledger.run_journal import JOURNAL_FILE_NAME, StepRecord, UnsupportedRunJournalRecord
from mote.runtime.persistence import disk_io
from mote.runtime.session.workspace import SessionSpace, SessionWorkspace


def _journal(tmp_path, session_id: str = "session") -> RunJournal:
    return RunJournal(session_id, SessionWorkspace(root=tmp_path))


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EACCES, errno.EIO])
@pytest.mark.asyncio
async def test_started_failure_prevents_external_body(monkeypatch, tmp_path, code):
    journal = _journal(tmp_path)
    calls = 0

    def fail_append(*args, **kwargs):
        raise OSError(code, "injected append failure")

    async def external_body() -> str:
        nonlocal calls
        calls += 1
        return "effect"

    monkeypatch.setattr(disk_io, "append_line", fail_append)
    with pytest.raises(LedgerPersistenceError, match="operation=append"):
        await run_journaled_step(journal, "call", KIND_TOOL, "external", external_body)
    assert calls == 0
    assert journal.replay("call") is None


def test_terminal_failure_preserves_started_in_memory_and_after_restart(monkeypatch, tmp_path):
    journal = _journal(tmp_path)
    journal.record_started("call", KIND_TOOL, "external")

    def fail_append(*args, **kwargs):
        raise OSError(errno.EIO, "injected terminal fsync failure")

    monkeypatch.setattr(disk_io, "append_line", fail_append)
    with pytest.raises(LedgerPersistenceError):
        journal.record_completed("call", payload="remote success")
    assert journal.replay("call").status == STARTED
    assert _journal(tmp_path).replay("call").status == STARTED


def test_reap_failure_preserves_old_memory_and_disk(monkeypatch, tmp_path):
    journal = _journal(tmp_path)
    journal.record_started("call", KIND_TOOL, "external")
    journal.record_completed("call", payload="done")

    def fail_rewrite(*args, **kwargs):
        raise OSError(errno.ENOSPC, "injected rewrite failure")

    monkeypatch.setattr(disk_io, "atomic_write", fail_rewrite)
    with pytest.raises(LedgerPersistenceError, match="operation=reap"):
        journal.reap(["call"])
    assert journal.replay("call") is not None
    assert _journal(tmp_path).replay("call") is not None


def test_middle_corruption_reports_location(tmp_path):
    journal = _journal(tmp_path)
    journal.record_started("first", KIND_TOOL, "external")
    with journal.path.open("ab") as stream:
        stream.write(b"not-json\n")
    with pytest.raises(LedgerCorruptionError) as caught:
        _journal(tmp_path)
    assert caught.value.line_number == 2
    assert caught.value.byte_offset > 0


def test_unterminated_tail_is_ignored_as_uncommitted(tmp_path):
    journal = _journal(tmp_path)
    journal.record_started("first", KIND_TOOL, "external")
    with journal.path.open("ab") as stream:
        stream.write(b'{"step_id":"second"')
    rebuilt = _journal(tmp_path)
    assert rebuilt.replay("first") is not None
    assert rebuilt.replay("second") is None


def test_lifecycle_fork_fails_during_append_and_fold(tmp_path):
    journal = _journal(tmp_path)
    journal.record_started("call", KIND_TOOL, "external")
    with pytest.raises(RunJournalLifecycleError):
        journal.record_started("call", KIND_TOOL, "external")

    fork = StepRecord(step_id="call", kind=KIND_TOOL, effect="external", status=STARTED)
    with journal.path.open("ab") as stream:
        stream.write((fork.to_json() + "\n").encode())
    with pytest.raises(LedgerCorruptionError, match="run_journal_lifecycle"):
        _journal(tmp_path)


def test_terminal_without_started_fails_closed(tmp_path):
    with pytest.raises(RunJournalLifecycleError, match="terminal_without_started"):
        _journal(tmp_path).record_completed("missing", payload="result")


def test_append_rejects_record_its_strict_reader_would_reject(tmp_path):
    journal = _journal(tmp_path)
    invalid = StepRecord(
        step_id="invalid",
        kind="unknown",
        effect="external",
        status=STARTED,
    )

    with pytest.raises(UnsupportedRunJournalRecord, match="supported run-step kind"):
        journal.append(invalid)
    assert journal.replay("invalid") is None
    assert not journal.path.exists()


def test_parent_creation_failure_is_typed(monkeypatch, tmp_path):
    journal = _journal(tmp_path)

    def fail_mkdir(*args, **kwargs):
        raise OSError(errno.EACCES, "injected parent mkdir failure")

    monkeypatch.setattr(type(journal.path), "mkdir", fail_mkdir)
    with pytest.raises(LedgerPersistenceError, match="operation=append"):
        journal.record_started("call", KIND_TOOL, "external")
