"""GitContextSource — a point-in-time git working-tree snapshot.

Not a live per-turn feed: the working tree is *not* re-polled every cycle.
Instead a snapshot is captured at exactly two moments and surfaced once each:

- **session start** — so the model opens with an accurate picture of the branch
  / dirty-clean state / recent commits it inherited;
- **after a compaction** — the snapshot is re-captured and re-shown, because the
  earlier one was condensed away with the rest of the pre-compaction history.

Between those events the block is silent. The rendered text says so plainly
("point-in-time snapshot ... run ``git status`` for the current state"), handing
the responsibility for *current* state back to the model rather than pretending
the framework tracks it live. This follows a "snapshot in time, will
not update during the conversation" contract and avoids both the per-turn ``git``
subprocess cost and the false-freshness trap of a change-gated live feed.

Push→pull bridge in one object (like ``CompactionNoticeContextSource``):
- as an :class:`~mote.common.interface.ObservationSubscriber` it catches
  :class:`SessionStartEvent` / :class:`PostCompactEvent` off the bus and *freezes*
  the snapshot **at that instant** (so the block honestly reflects the moment it
  claims, not whatever the tree looks like a turn later);
- as an :class:`~mote.common.interface.EphemeralContextSource` it renders the
  frozen snapshot once on the next think() cycle, then disarms.

Best-effort throughout: ``collect_git_state`` returns ``None`` off-repo / on any
failure. ``None`` is a *legitimate* snapshot result ("not in a repo at this
moment") — it is faithfully rendered as nothing and the notice disarms; it is
never retried, because "the cwd became a repo later" is not one of the two
capture events.
"""

from __future__ import annotations

from typing import Callable, Optional

from mote.common.events import PostCompactEvent, SessionStartEvent
from mote.common.interface import ObservationSubscriber, TurnContextPriority
from mote.common.logs import logger
from mote.common.utils.git_state import collect_git_state, render_git_section

# Zero-arg provider of the *current* working directory (PostCompactEvent carries
# no cwd, so the source is handed one to resolve the tree at compaction time).
CwdProvider = Callable[[], Optional[str]]

_SNAPSHOT_FOOTER = (
    "\n(Point-in-time snapshot; it will not update as you work. "
    "Run `git status` / `git diff` for the current state.)"
)


class GitContextSource(ObservationSubscriber):
    """Freezes a git snapshot at session-start / post-compaction, renders it once."""

    name = "git"
    # Render order in the turn-context bus (lowest first): git leads the block.
    # The same value serves as the ObservationSubscriber dispatch priority, where
    # it is immaterial (this handler only observes — it returns no outcome).
    priority = TurnContextPriority.GIT
    save_to_context = True

    def __init__(self, get_cwd: Optional[CwdProvider] = None) -> None:
        # The frozen, already-rendered snapshot text (None once captured off-repo
        # or on failure). ``_pending`` gates whether render() should emit it.
        self._get_cwd = get_cwd
        self._snapshot: Optional[str] = None
        self._pending = False

    async def handle(self, event) -> None:
        """Freeze the snapshot on a capture event; ignore everything else."""
        if isinstance(event, SessionStartEvent):
            # SessionStartEvent carries its own working_dir — capture the tree the
            # session actually opened in (may differ from a later cwd).
            await self._capture(event.working_dir or None)
        elif isinstance(event, PostCompactEvent):
            # PostCompactEvent carries no cwd; resolve the live one via the
            # injected provider (the cwd may have moved since session start).
            cwd = self._get_cwd() if self._get_cwd else None
            await self._capture(cwd)
        return None

    async def _capture(self, cwd: Optional[str]) -> None:
        """Freeze the git snapshot for *cwd* at this instant and arm the notice.

        ``None`` (off-repo / any failure) is a faithful snapshot of "not in a repo
        right now": the block renders as nothing, but the notice is still armed and
        disarms on the next render — no retry, no live tracking.
        """
        block: Optional[str] = None
        try:
            state = await collect_git_state(cwd) if cwd else None
            if state is not None:
                block = render_git_section(state) or None
        except Exception as exc:  # noqa: BLE001 — best-effort; never break a turn
            logger.warning(f"git snapshot capture failed: {exc}")
            block = None
        self._snapshot = block
        self._pending = True

    async def render(self, *, cwd: Optional[str] = None) -> Optional[str]:
        if not self._pending:
            return None
        self._pending = False
        if not self._snapshot:
            return None
        return self._snapshot + _SNAPSHOT_FOOTER


__all__ = ["GitContextSource"]
