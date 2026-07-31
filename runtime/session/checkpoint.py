"""Rollout projection for whole-worktree checkpoints and committed rewinds."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import List, Optional

from mote.contracts.events.file.facts import (
    RewindAbortedEvent,
    RewindCommittedEvent,
    RewindInDoubtEvent,
    RewindPreparedEvent,
)
from mote.runtime.session.codec import decode_session_event
from mote.runtime.session.events import CheckpointEvent
from mote.runtime.session.log import SessionLog
from mote.runtime.vcs import find_git_root


def checkpoint_supported(working_dir: Optional[str]) -> bool:
    """Whether whole-worktree checkpoints can run for this workspace."""

    if shutil.which("git") is None:
        return False
    try:
        return find_git_root(working_dir or ".") is not None
    except OSError:
        return False


@dataclass
class CheckpointEntry:
    """One captured whole-tree checkpoint (a flattened :class:`CheckpointEvent`).

    ``index`` is this checkpoint's position within the session's checkpoint list
    (0-based, chronological — the value the user types to ``/rewind``); ``ts`` is
    the event's ISO timestamp. ``after_commit`` is the twin turn-end snapshot (the
    tree the agent left behind), folded in from the matching after-event by
    :func:`list_checkpoints`; empty when the turn had no recorded end (still
    in-flight, or a pre-``after_commit`` rollout). Diffing it against the live tree
    at rewind time surfaces files an external process changed after the agent
    finished.
    """

    commit: str
    prompt_index: int
    prompt_preview: str
    working_dir: str
    ts: str
    index: int
    after_commit: str = ""


def list_checkpoints(log: SessionLog) -> List[CheckpointEntry]:
    """Every ``checkpoint`` event in ``log``, chronological, with a 0-based ``index``.

    A forward scan of the same ``rollout.jsonl`` (mirrors
    :func:`mote.runtime.session.history.file_history`) — no second index. Each entry's
    ``index`` is its position in this list (the value the user types to
    ``/rewind``).

    Each user turn writes up to two ``CheckpointEvent``s: a *before* event (the
    tree entering the turn — carries ``commit``) and, at turn end, an *after* event
    (the tree the agent left — carries only ``after_commit``, ``commit=""``, same
    ``prompt_index``). Only before-events become entries; an after-event is folded
    onto the entry sharing its ``prompt_index`` (setting ``after_commit``), so a
    checkpoint knows both the tree it opened with and the tree the agent produced.
    """
    out: List[CheckpointEntry] = []
    by_prompt: dict = {}
    prepared_rewinds: dict[str, RewindPreparedEvent] = {}
    for envelope in log.iter_events():
        event = decode_session_event(envelope)
        if isinstance(event, RewindPreparedEvent):
            prepared_rewinds[event.transaction_id] = event
            continue
        if isinstance(event, RewindCommittedEvent):
            prepared = prepared_rewinds.pop(event.transaction_id, None)
            if prepared is not None:
                out.append(
                    CheckpointEntry(
                        commit=prepared.safety_commit,
                        prompt_index=prepared.prompt_index,
                        prompt_preview="(before rewind)",
                        working_dir=prepared.working_dir,
                        ts=envelope.occurred_at.isoformat(),
                        index=len(out),
                    )
                )
            continue
        if isinstance(event, (RewindAbortedEvent, RewindInDoubtEvent)):
            prepared_rewinds.pop(event.transaction_id, None)
            continue
        if not isinstance(event, CheckpointEvent):
            continue
        if event.commit:
            entry = CheckpointEntry(
                commit=event.commit,
                prompt_index=event.prompt_index,
                prompt_preview=event.prompt_preview,
                working_dir=event.working_dir,
                ts=envelope.occurred_at.isoformat(),
                index=len(out),
                after_commit=event.after_commit,
            )
            out.append(entry)
            by_prompt[event.prompt_index] = entry
        elif event.after_commit:
            entry = by_prompt.get(event.prompt_index)
            if entry is not None:
                entry.after_commit = event.after_commit
    return out


__all__ = ["CheckpointEntry", "checkpoint_supported", "list_checkpoints"]
