"""ChangedFilesContextSource — flag files edited on disk since we last read them.

The agent's view of a file is the bytes it read when it last ran Read; the
``record_file_read`` trajectory stores the file's ``mtime_ns`` at that moment
(``RoleState._file_read_state``). If something *outside* the agent's own
Read/Edit/Write cycle rewrites the file — the user editing in their IDE, a
formatter, a git operation, a build step — the agent's cached view goes stale
without it noticing. This source compares each tracked file's current on-disk
mtime against the recorded one and, per turn, injects a ``<system-reminder>``
naming the files that changed, so the model knows to re-read before trusting its
memory of them.

The agent's own edits do NOT trip this: the Edit/Write tools refresh the read
state to the just-written mtime, so only external changes surface.

Change-gated: a file is reported once per detected change (its new mtime is
remembered), so the same stale file is not re-announced every turn until it
changes again.

Duck-typed (mirrors :class:`SkillActivationContextSource`): it holds one plain
callable returning the ``{path: recorded_mtime_ns}`` map, so the low ``context``
layer never imports the Role.
"""

from __future__ import annotations

from typing import Callable, Optional

from mote.common.disk import mtime_ns
from mote.common.interface import TurnContextPriority
from mote.common.text import display_path

# A zero-arg callable returning a snapshot of the session's file-read state:
# ``{absolute_path: mtime_ns_when_last_read}``. Matches ``RoleState._file_read_state``.
ReadStateProvider = Callable[[], dict]


class ChangedFilesContextSource:
    """Emits a reminder listing tracked files changed on disk since last read."""

    name = "changed_files"
    # Between the post-compaction notice and background tasks: a freshness
    # warning is mid-urgency — more actionable than skill hints, less than a
    # just-happened compaction.
    priority = TurnContextPriority.CHANGED_FILES
    # Ephemeral (request-only): a "re-read this stale file" warning is a one-shot
    # nudge (change-gated: announced once per detected revision). Once the model
    # has been told, persisting it adds nothing — the freshness fact is only
    # actionable on the turn it surfaces.
    save_to_context = False

    def __init__(self, get_read_state: ReadStateProvider) -> None:
        self._get_read_state = get_read_state
        # path -> mtime_ns we last *reported* as changed, so a file stays quiet
        # after being announced until it changes again.
        self._reported: dict[str, int] = {}

    async def render(self, *, cwd: Optional[str] = None) -> Optional[str]:
        read_state = self._get_read_state() if self._get_read_state else None
        if not read_state:
            return None

        changed: list[str] = []
        for path, read_mtime in read_state.items():
            current = mtime_ns(path)
            if current is None or current == read_mtime:
                continue  # gone/unreadable or unchanged since we read it
            if self._reported.get(path) == current:
                continue  # already announced this exact revision
            self._reported[path] = current
            changed.append(path)

        if not changed:
            return None

        lines = [
            "# Files changed on disk",
            "These files changed outside your edits since you last read them; your "
            "cached view is stale. Re-read before relying on their contents:",
            "",
        ]
        lines.extend(f"- {display_path(p, cwd)}" for p in changed)
        return "\n".join(lines)


__all__ = ["ChangedFilesContextSource", "ReadStateProvider"]
