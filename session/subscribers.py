"""RecorderSubscriber — the durable-log sink, now an event-bus subscriber.

Replaces ``session/recorder.py``'s ``SessionRecorder``: instead of being a sink
injected into ``ContextManager``, it subscribes to the unified event bus and
maps the agent's lifecycle events (message / compaction / turn-end) to
``session/events.py`` records appended to a :class:`SessionLog`. Screen (renderer
subscriber) and disk (this) are now fed by the *same* event stream, so they can
no longer diverge.

The ``session_meta`` first line is **not** written here: the
:attr:`~mote.roles.role_components.RoleComponents.session_log` property writes
it when it builds the log (before this subscriber is even constructed), so meta
has a single source of truth and this sink only appends.

It runs at a **high priority** so it persists after the hook subscriber has had
its say (a vetoed action is never recorded as having happened).

``enabled`` gates recording (turned off while replaying a resumed session).

This is the **durable** sink (``delivery = DURABLE``): unlike a mirror observer,
its failures are *not* swallowed here. The bus's durable branch surfaces them —
logged loud and counted in :attr:`~mote.common.events.bus.EventBus.durable_failures`
— because a dropped rollout record is real data loss, not a cosmetic mirror miss.
The bus also never times this sink out: it must complete.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable, Optional

from mote.common.disk import get_disk_writer
from mote.common.disk.async_io import run_disk_io
from mote.common.events.types import (
    CompactionCheckpointEvent,
    HistoryEditedEvent,
    LLMResponseEvent,
    MessageAppendedEvent,
    PostToolUseEvent,
    TurnEndEvent,
    UserPromptSubmitEvent,
)
from mote.common.interface.event_subscriber import DURABLE, DeliveryPolicy, ObservationSubscriber, ObserverPriority
from mote.common.logs import log_class, logger
from mote.common.text.hashing import content_hash
from mote.session.checkpoint import CheckpointStore, list_checkpoints
from mote.session.events import (
    CheckpointEvent,
    CompactedEvent,
    LLMCallEvent,
    MessageEvent,
    MetaUpdateEvent,
    TurnContextEvent,
    parse_event,
)
from mote.session.hunk_ledger import AGENT, HunkLedger
from mote.session.log import SessionLog
from mote.session.snapshot import GIT_DIRNAME

#: How many leading chars of the user prompt to stash for the /rewind listing.
_PREVIEW_LEN = 200


@log_class(level="DEBUG", exclude={"handle"})
class RecorderSubscriber(ObservationSubscriber):
    """Streams bus events to a :class:`SessionLog` (the session rollout)."""

    #: Run after the hook subscriber so vetoes are folded before we persist.
    priority: int = ObserverPriority.PERSIST
    #: Opt into durable delivery: never time-boxed, failures surfaced not dropped.
    delivery: DeliveryPolicy = DURABLE

    def __init__(self, log: SessionLog, *, enabled: bool = True):
        self._log = log
        self.enabled = enabled

    @property
    def log(self) -> SessionLog:
        return self._log

    async def handle(self, event) -> None:
        if not self.enabled:
            return
        if isinstance(event, MessageAppendedEvent):
            if event.message is not None:
                self._log.append(MessageEvent(message=event.message))
        elif isinstance(event, LLMResponseEvent):
            # Compact per-request telemetry: token usage + cost only (the
            # prompt/completion already land as message records). Skip the
            # no-usage placeholder calls so the rollout isn't polluted.
            if event.usage is not None:
                self._log.append(
                    LLMCallEvent(
                        request_id=event.request_id,
                        model=event.model,
                        usage=event.usage,
                        cost_usd=event.cost_usd,
                        latency_ms=event.latency_ms,
                    )
                )
        elif isinstance(event, CompactionCheckpointEvent):
            self._log.append(CompactedEvent(messages=list(event.messages), summary=event.summary or ""))
        elif isinstance(event, HistoryEditedEvent):
            # A direct user edit (e.g. deleted react-units): persisted as a
            # CompactedEvent so replay/resume reset history to the pruned list
            # for free. No summary — this is not a compaction, and the view
            # projector ignores HistoryEditedEvent so no boundary marker shows.
            self._log.append(CompactedEvent(messages=list(event.messages), summary=""))
        elif isinstance(event, TurnEndEvent):
            self._log.append(
                TurnContextEvent(
                    turn_id=event.turn_id,
                    working_dir=event.working_dir,
                    model=event.model,
                    token_state=event.token_state,
                )
            )
            # Durability checkpoint: flush this turn's queued writes to disk
            # so the rollout is complete at the turn boundary (a crash before
            # the next turn loses only an in-progress, unfinished turn).

            await get_disk_writer().drain()


#: Cap the prompt fed to the title model so a giant paste never bloats the call.
_TITLE_PROMPT_LEN = 2000


@log_class(level="DEBUG", exclude={"handle"})
class TitleSubscriber(ObservationSubscriber):
    """Generates a one-line session title from the first user prompt (once).

    Persistence (``MetaUpdateEvent.title`` appended at the log tail) and read-back
    (``session/listing.py`` reads the newest title from the tail window) already
    existed; the *generator* was the missing piece. This subscriber closes it: on
    the first **live** :class:`UserPromptSubmitEvent` it fires a single cheap
    auxiliary-model call and appends a :class:`MetaUpdateEvent` carrying the title
    (plus a preview of the prompt that seeded it).

    Once-per-session, resume-safe: the ``_done`` latch is seeded at build by
    scanning the existing log for an already-titled ``MetaUpdateEvent`` (mirroring
    how :class:`CheckpointSubscriber` seeds ``_prompt_index`` from
    ``list_checkpoints``), so a resumed session that was already titled never
    re-generates. Replay/resume does not re-emit ``UserPromptSubmitEvent``, so the
    first live prompt is the only trigger.

    Non-blocking: the bus **awaits** an observer's ``handle`` (MIRROR observers are
    time-boxed), so the LLM latency must never ride inside it. ``handle`` only
    latches + spawns a tracked background task; the model call and the append run
    off the dispatch path. A dropped title is purely cosmetic (MIRROR delivery,
    ``BOOKKEEPING`` priority), so any failure is logged and swallowed.

    The title model is injected as ``generate`` (``prompt -> title``) so ``session``
    never imports ``router``; the role layer wires it to a cheap task-routed call.
    ``enabled`` gates the whole feature (off for ephemeral child roles).
    """

    #: Pure internal bookkeeping: a lost title is cosmetic, never correctness.
    priority: int = ObserverPriority.BOOKKEEPING

    def __init__(
        self,
        log: SessionLog,
        generate: Callable[[str], Awaitable[Optional[str]]],
        *,
        enabled: bool = True,
    ):
        self._log = log
        self._generate = generate
        self.enabled = enabled
        # Resume guard: a session already carrying a title must never re-title.
        self._done = _has_title(log)
        # Hold a strong ref so the fire-and-forget task isn't GC'd mid-flight.
        self._task: Optional[asyncio.Task] = None

    @property
    def log(self) -> SessionLog:
        return self._log

    async def handle(self, event) -> None:
        if not self.enabled or self._done:
            return
        if not isinstance(event, UserPromptSubmitEvent):
            return
        prompt = (event.prompt or "").strip()
        if not prompt:
            return
        # Latch before spawning so a rapid second prompt cannot double-fire.
        self._done = True
        self._task = asyncio.create_task(self._make_title(prompt))

    async def _make_title(self, prompt: str) -> None:
        try:
            title = await self._generate(prompt[:_TITLE_PROMPT_LEN])
        except Exception as exc:  # noqa: BLE001 — a cosmetic title never breaks a turn
            logger.debug(f"title generation failed: {exc}")
            return
        title = (title or "").strip()
        if not title:
            return
        self._log.append(MetaUpdateEvent(title=title, last_prompt=prompt[:_PREVIEW_LEN]))


def _has_title(log: SessionLog) -> bool:
    """True when the log already carries a titled ``MetaUpdateEvent`` (resume)."""
    if not log.exists():
        return False
    for record in log.iter_raw():
        event = parse_event(record)
        if isinstance(event, MetaUpdateEvent) and (event.title or "").strip():
            return True
    return False


@log_class(level="DEBUG", exclude={"handle"})
class CheckpointSubscriber(ObservationSubscriber):
    """Captures whole-tree checkpoints at each user-turn boundary (``/rewind``).

    Two snapshots bracket every turn. On :class:`UserPromptSubmitEvent` it
    snapshots the live working tree (the *before* image — the tree the turn opens
    with) into the session's dedicated ``{session}/git`` repo (via
    :class:`~mote.session.checkpoint.CheckpointStore`) and appends a
    :class:`CheckpointEvent`. On the matching :class:`TurnEndEvent` it snapshots
    again (the *after* image — the tree the agent left) and appends a second
    ``CheckpointEvent`` carrying only ``after_commit`` (``commit=""``, same
    ``prompt_index``); :func:`~mote.session.checkpoint.list_checkpoints` folds the
    two together. The after-image lets ``/rewind`` diff the agent's result against
    the live tree to flag files an external process touched since. Observation-only:
    it never vetoes the turn.

    Best-effort throughout (MIRROR delivery): a capture failure is dropped, never
    surfaced as data loss — losing a rollback point must not break the turn. The
    ``prompt_index`` counter is seeded from the existing checkpoint count at
    build so it stays monotonic across resume, and each snapshot is parented on the
    last commit so the before/after images chain into one linear history.
    """

    #: Persist tier: it writes to the rollout log, after any control veto folds.
    priority: int = ObserverPriority.PERSIST

    def __init__(
        self,
        log: SessionLog,
        get_working_dir: Callable[[], str],
        *,
        enabled: bool = True,
    ):
        self._log = log
        self._get_working_dir = get_working_dir
        self.enabled = enabled
        self._git_dir = log.path.parent / GIT_DIRNAME
        existing = list_checkpoints(log)
        self._prompt_index = len(existing)
        self._last_commit: Optional[str] = existing[-1].commit if existing else None
        # The prompt_index of the turn currently in flight, awaiting its after-image
        # at TurnEndEvent (None between turns, so a stray TurnEndEvent is a no-op).
        self._pending_after: Optional[int] = None

    @property
    def log(self) -> SessionLog:
        return self._log

    async def handle(self, event) -> None:
        if not self.enabled:
            return
        if isinstance(event, UserPromptSubmitEvent):
            await self._capture_before(event)
        elif isinstance(event, TurnEndEvent):
            await self._capture_after()

    async def _capture_before(self, event: UserPromptSubmitEvent) -> None:
        work_dir = self._get_working_dir()
        if not work_dir:
            return
        store = CheckpointStore(self._git_dir, Path(work_dir))
        index = self._prompt_index
        commit = await run_disk_io(
            store.capture,
            parent=self._last_commit,
            message=f"checkpoint {index}",
        )
        if commit is None:
            return  # capture failed (non-repo, git error) — feature stays inert
        self._last_commit = commit
        self._prompt_index += 1
        self._pending_after = index
        self._log.append(
            CheckpointEvent(
                commit=commit,
                prompt_index=index,
                prompt_preview=(event.prompt or "")[:_PREVIEW_LEN],
                working_dir=work_dir,
            )
        )

    async def _capture_after(self) -> None:
        if self._pending_after is None:
            return  # no in-flight turn (e.g. a TurnEndEvent before any prompt)
        work_dir = self._get_working_dir()
        if not work_dir:
            return
        store = CheckpointStore(self._git_dir, Path(work_dir))
        index = self._pending_after
        commit = await run_disk_io(
            store.capture,
            parent=self._last_commit,
            message=f"checkpoint {index} after",
        )
        self._pending_after = None
        if commit is None:
            return
        self._last_commit = commit
        self._log.append(
            CheckpointEvent(
                commit="",
                prompt_index=index,
                working_dir=work_dir,
                after_commit=commit,
            )
        )


@log_class(level="DEBUG", exclude={"handle"})
class HunkSubscriber(ObservationSubscriber):
    """Derives *agent* change hunks at the tool-settle point and ledgers them.

    Subscribes to :class:`PostToolUseEvent`, which the executor emits at its
    settle chokepoint carrying the tool's structured ``file_changes`` (each a
    ``FileChange(path, old, new)``). For every successful file-mutating result it
    hands the old→new delta to :meth:`HunkLedger.record_delta`, which splits it
    into contiguous hunks and appends one ``source=agent`` record per hunk,
    stamped with the current ``turn_index`` (read live via the injected getter)
    and the tool call's id.

    Pure observation: it never vetoes or rewrites — it only records what changed,
    exactly like the rollout/checkpoint recorders. It reuses the ``FileChange``
    fact the executor already produces (no new capture path). The ``old`` side is
    stored via the injected blob store and keyed by that store's own returned
    digest, so the before-image is fetchable back under any backend (the store's
    ``put`` is idempotent, so it dedups against the before-image the snapshot
    recorder already holds). The hunk text is never duplicated in the ledger; it
    is reconstructed on demand from that blob plus the live file.

    ``enabled`` gates recording (off while replaying a resumed session, and when
    the role opts out via ``record_hunks``).
    """

    #: Persist tier: record after any control veto has folded (a blocked call's
    #: file change, if the body already wrote it, is still a real on-disk change).
    priority: int = ObserverPriority.PERSIST

    def __init__(
        self,
        ledger: HunkLedger,
        get_turn_index: Callable[[], int],
        session_id: str,
        blobs,
        *,
        enabled: bool = True,
    ):
        self._ledger = ledger
        self._get_turn_index = get_turn_index
        self._session_id = session_id
        self._blobs = blobs
        self.enabled = enabled

    @property
    def ledger(self) -> HunkLedger:
        return self._ledger

    async def handle(self, event) -> None:
        if not self.enabled:
            return
        if not isinstance(event, PostToolUseEvent):
            return
        # Only successful, file-mutating results carry a real on-disk change to
        # attribute; a failed call or a non-mutating tool has nothing to split.
        if not event.success or not event.file_changes:
            return
        turn_index = self._get_turn_index()
        for change in event.file_changes:
            self._record_change(event.tool_use_id, change, turn_index)

    def _record_change(self, tool_use_id, change, turn_index: int) -> None:
        old = getattr(change, "old", "") or ""
        new = getattr(change, "new", "") or ""
        path = getattr(change, "path", "") or ""
        # A stable id base so a resume that replays the same tool call folds onto
        # the same records (idempotent) rather than duplicating them. Native
        # channel supplies a unique ``tool_use_id``; the XML channel has none, so
        # fall back to a content fingerprint (stable per change, and XML sessions
        # do not replay a call under the same id anyway).
        base = tool_use_id or content_hash(f"{path}|{content_hash(old)}|{content_hash(new)}")
        # Include the path in the id: one tool call may touch several files, so
        # ``base`` (the call id) alone is not unique per hunk — two files' first
        # hunks would both be ``{call}:0`` and the second would fold over the
        # first in the ledger index. ``{base}:{path}:{i}`` stays both unique
        # across a call's files and stable across a resume replay of that call.
        path_key = content_hash(path)
        self._ledger.record_delta(
            self._blobs,
            path=path,
            old=old,
            new=new,
            source=AGENT,
            turn_index=turn_index,
            tool_call_id=tool_use_id or "",
            id_base=f"{base}:{path_key}",
        )


__all__ = ["RecorderSubscriber", "TitleSubscriber", "CheckpointSubscriber", "HunkSubscriber"]
