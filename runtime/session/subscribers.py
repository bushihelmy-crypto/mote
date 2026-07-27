"""Session-owned live observers for titles and worktree checkpoints."""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

from mote.contracts.events.types import TurnEndEvent, UserPromptSubmitEvent
from mote.contracts.fileops import RewindFailedError
from mote.runtime.disk.async_io import run_disk_io
from mote.runtime.logging import log_class, logger
from mote.runtime.session.checkpoint import list_checkpoints
from mote.runtime.session.codec import decode_session_event
from mote.runtime.session.events import CheckpointEvent, MetaUpdateEvent
from mote.runtime.session.log import SessionLog

#: How many leading chars of the user prompt to stash for the /rewind listing.
_PREVIEW_LEN = 200


#: Cap the prompt fed to the title model so a giant paste never bloats the call.
_TITLE_PROMPT_LEN = 2000


@log_class(level="DEBUG", exclude={"handle"})
class TitleSubscriber:
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

    Non-blocking: ``handle`` only
    latches + spawns a tracked background task; the model call and the append run
    off the dispatch path. A dropped title is purely cosmetic, so any failure is
    logged and swallowed.

    The title model is injected as ``generate`` (``prompt -> title``) so ``session``
    never imports ``router``; the role layer wires it to a cheap task-routed call.
    ``enabled`` gates the whole feature (off for ephemeral child roles).
    """

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
        await self._log.append(MetaUpdateEvent(title=title, last_prompt=prompt[:_PREVIEW_LEN]))

    async def aclose(self) -> None:
        """Cancel and join the owned title task, if one is still running."""
        task, self._task = self._task, None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def _has_title(log: SessionLog) -> bool:
    """True when the log already carries a titled ``MetaUpdateEvent`` (resume)."""
    if not log.exists():
        return False
    for envelope in log.iter_events():
        event = decode_session_event(envelope)
        if isinstance(event, MetaUpdateEvent) and (event.title or "").strip():
            return True
    return False


@log_class(level="DEBUG", exclude={"handle"})
class CheckpointSubscriber:
    """Captures whole-tree checkpoints at each user-turn boundary (``/rewind``).

    Two snapshots bracket every turn. On :class:`UserPromptSubmitEvent` it
    snapshots the live working tree (the *before* image — the tree the turn opens
    with) through the File Operations project barrier into the session's
    dedicated ``{session}/git`` repo and appends a
    :class:`CheckpointEvent`. On the matching :class:`TurnEndEvent` it snapshots
    again (the *after* image — the tree the agent left) and appends a second
    ``CheckpointEvent`` carrying only ``after_commit`` (``commit=""``, same
    ``prompt_index``); :func:`~mote.runtime.session.checkpoint.list_checkpoints` folds the
    two together. The after-image lets ``/rewind`` diff the agent's result against
    the live tree to flag files an external process touched since.

    Best-effort throughout: a capture failure is dropped, never
    surfaced as data loss — losing a rollback point must not break the turn. The
    ``prompt_index`` counter is seeded from the existing checkpoint count at
    build so it stays monotonic across resume, and each snapshot is parented on the
    last commit so the before/after images chain into one linear history.
    """

    def __init__(
        self,
        log: SessionLog,
        get_working_dir: Callable[[], str],
        capture_checkpoint: Callable[..., str],
        *,
        enabled: bool = True,
    ):
        self._log = log
        self._get_working_dir = get_working_dir
        self._capture_checkpoint = capture_checkpoint
        self.enabled = enabled
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
        index = self._prompt_index
        try:
            commit = await run_disk_io(
                self._capture_checkpoint,
                working_dir=work_dir,
                parent_commit=self._last_commit,
                message=f"checkpoint {index}",
            )
        except RewindFailedError:
            return
        self._last_commit = commit
        self._prompt_index += 1
        self._pending_after = index
        await self._log.append(
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
        index = self._pending_after
        try:
            commit = await run_disk_io(
                self._capture_checkpoint,
                working_dir=work_dir,
                parent_commit=self._last_commit,
                message=f"checkpoint {index} after",
            )
        except RewindFailedError:
            commit = None
        self._pending_after = None
        if commit is None:
            return
        self._last_commit = commit
        await self._log.append(
            CheckpointEvent(
                commit="",
                prompt_index=index,
                working_dir=work_dir,
                after_commit=commit,
            )
        )


__all__ = [
    "TitleSubscriber",
    "CheckpointSubscriber",
]
