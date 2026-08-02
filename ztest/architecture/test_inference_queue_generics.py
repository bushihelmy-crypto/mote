from __future__ import annotations

from pathlib import Path
from typing import get_type_hints

from mote.runtime.inference.fair_queue import FairAdmissionQueue, QueueEntry


def test_queue_entry_payload_and_queue_are_generic() -> None:
    payload = get_type_hints(QueueEntry)["payload"]

    assert str(payload).endswith("PayloadT")
    assert QueueEntry.__parameters__
    assert FairAdmissionQueue.__parameters__


def test_queue_and_dispatcher_do_not_erase_or_recover_payload_type() -> None:
    for relative in (
        "runtime/inference/fair_queue.py",
        "runtime/inference/dispatcher.py",
    ):
        source = Path(relative).read_text(encoding="utf-8")
        assert "import Any" not in source
        assert "payload: Any" not in source
        assert "cast(" not in source


def test_each_runtime_binds_queue_entry_to_its_canonical_work_item() -> None:
    expected = {
        "runtime/inference/runtime.py": 'QueueEntry["_AttemptExecution"]',
        "runtime/inference/command_runtime.py": 'QueueEntry["_CommandExecution"]',
        "runtime/inference/session_runtime.py": "QueueEntry[_SessionWork]",
    }

    for relative, annotation in expected.items():
        source = Path(relative).read_text(encoding="utf-8")
        assert annotation in source
