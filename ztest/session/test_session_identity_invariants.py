from __future__ import annotations

from pathlib import Path

import pytest

from mote.contracts.conversation import UserMessage
from mote.runtime.session.codec import encode_session_event
from mote.runtime.session.events import MessageEvent, SessionMetaEvent
from mote.runtime.session.log import SessionLog
from mote.runtime.session.projection import (
    SessionProjectionIdentityError,
    SessionProjectionState,
    reduce_session_envelope,
)


def _append_direct(log: SessionLog, event, *, envelope_session_id: str, expected: int) -> None:
    fact = encode_session_event(event, session_id=envelope_session_id)
    log.event_journal.append_committed(log.stream_id, (fact,), expected_version=expected)


def test_verified_read_rejects_missing_first_metadata(tmp_path: Path) -> None:
    log = SessionLog("session-1", base_dir=str(tmp_path))
    _append_direct(
        log,
        MessageEvent(message=UserMessage("orphan")),
        envelope_session_id="session-1",
        expected=0,
    )
    with pytest.raises(RuntimeError, match="missing its first metadata"):
        tuple(log.iter_events())


def test_verified_read_rejects_duplicate_or_late_metadata(tmp_path: Path) -> None:
    log = SessionLog("session-1", base_dir=str(tmp_path))
    meta = SessionMetaEvent("session-1", "fake.agent.v1", ())
    _append_direct(log, meta, envelope_session_id="session-1", expected=0)
    _append_direct(log, meta, envelope_session_id="session-1", expected=1)
    with pytest.raises(RuntimeError, match="unique first fact"):
        tuple(log.iter_events())


def test_verified_read_rejects_meta_directory_and_envelope_identity_mismatch(
    tmp_path: Path,
) -> None:
    log = SessionLog("session-1", base_dir=str(tmp_path))
    _append_direct(
        log,
        SessionMetaEvent("session-2", "fake.agent.v1", ()),
        envelope_session_id="session-1",
        expected=0,
    )
    with pytest.raises(RuntimeError, match="metadata identity"):
        tuple(log.iter_events())


def test_live_projection_reasserts_stream_meta_identity(tmp_path: Path) -> None:
    log = SessionLog("session-1", base_dir=str(tmp_path))
    _append_direct(
        log,
        SessionMetaEvent("session-2", "fake.agent.v1", ()),
        envelope_session_id="session-1",
        expected=0,
    )
    envelope = next(log.event_journal.iter_committed(log.stream_id))
    with pytest.raises(SessionProjectionIdentityError, match="identities differ"):
        reduce_session_envelope(SessionProjectionState(), envelope)
