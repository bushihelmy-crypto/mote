from __future__ import annotations

import pytest

from mote.contracts.events.file.facts import FileTransactionAbortedEvent
from mote.runtime.fileops.journal import DurableFileOperationsJournal
from mote.runtime.fileops.locking import HierarchicalLockManager


def test_injected_event_port_replaces_the_file_backend(tmp_path) -> None:
    path = tmp_path / "rollout.jsonl"
    events = []
    journal = DurableFileOperationsJournal(
        path,
        session_id="session",
        locks=HierarchicalLockManager(tmp_path / "locks"),
        event_sink=events.append,
        event_source=lambda: tuple(events),
    )
    event = FileTransactionAbortedEvent("transaction", "cancelled")

    journal.append(event)

    assert tuple(journal.iter_events()) == (event,)
    assert not path.exists()


@pytest.mark.parametrize("missing", ["sink", "source"])
def test_injected_event_port_requires_both_directions(tmp_path, missing: str) -> None:
    events = []
    kwargs = {
        "event_sink": None if missing == "sink" else events.append,
        "event_source": None if missing == "source" else lambda: tuple(events),
    }

    with pytest.raises(ValueError, match="provided together"):
        DurableFileOperationsJournal(
            tmp_path / "journal.jsonl",
            session_id="session",
            locks=HierarchicalLockManager(tmp_path / "locks"),
            **kwargs,
        )
