"""JSONL record codec owned by the File contract."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from mote.contracts.events.file.facts import (
    FILE_EDIT_PLAN_STORED,
    FILE_HISTORY_IMPORTED,
    FILE_TRANSACTION_ABORTED,
    FILE_TRANSACTION_COMMITTED,
    FILE_TRANSACTION_IN_DOUBT,
    FILE_TRANSACTION_PREPARED,
    HUNK_DETECTED,
    HUNK_REVIEW_TRANSITIONED,
    REWIND_ABORTED,
    REWIND_COMMITTED,
    REWIND_IN_DOUBT,
    REWIND_PREPARED,
    FileEditPlanStoredEvent,
    FileHistoryImportedEvent,
    FileOperationsEvent,
    FileTransactionAbortedEvent,
    FileTransactionCommittedEvent,
    FileTransactionInDoubtEvent,
    FileTransactionPreparedEvent,
    HunkDetectedEvent,
    HunkReviewTransitionedEvent,
    RewindAbortedEvent,
    RewindCommittedEvent,
    RewindInDoubtEvent,
    RewindPreparedEvent,
)

_EVENT_TYPES = {
    FILE_HISTORY_IMPORTED: FileHistoryImportedEvent,
    FILE_EDIT_PLAN_STORED: FileEditPlanStoredEvent,
    FILE_TRANSACTION_PREPARED: FileTransactionPreparedEvent,
    FILE_TRANSACTION_COMMITTED: FileTransactionCommittedEvent,
    FILE_TRANSACTION_ABORTED: FileTransactionAbortedEvent,
    FILE_TRANSACTION_IN_DOUBT: FileTransactionInDoubtEvent,
    HUNK_DETECTED: HunkDetectedEvent,
    HUNK_REVIEW_TRANSITIONED: HunkReviewTransitionedEvent,
    REWIND_PREPARED: RewindPreparedEvent,
    REWIND_COMMITTED: RewindCommittedEvent,
    REWIND_ABORTED: RewindAbortedEvent,
    REWIND_IN_DOUBT: RewindInDoubtEvent,
}


def event_to_line(event: FileOperationsEvent) -> str:
    record = {
        "type": event.type,
        "ts": datetime.now().isoformat(),
        "payload": event.payload(),
    }
    return json.dumps(record, ensure_ascii=False)


def event_from_line(line: str) -> Optional[FileOperationsEvent]:
    try:
        record = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError("file operations journal line is not valid JSON") from exc
    if type(record) is not dict:
        raise ValueError("file operations journal record is not an object")
    event_type = record.get("type")
    if type(event_type) is not str:
        raise ValueError("file operations event type is invalid")
    event_class = _EVENT_TYPES.get(event_type)
    if event_class is None:
        raise ValueError(f"unsupported file operations event type: {event_type}")
    if set(record) != {"type", "ts", "payload"}:
        raise ValueError("file operations event envelope is not canonical")
    payload = record["payload"]
    if type(payload) is not dict:
        raise ValueError("file operations event payload is not an object")
    try:
        return event_class.from_payload(payload)
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError(f"file operations event payload is invalid: {event_type}") from exc


__all__ = ["event_from_line", "event_to_line"]
