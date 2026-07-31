"""Turn-context feed for typed externally attributed file changes."""

from __future__ import annotations

from typing import Optional

from mote.contracts.events.file.observation import FileChangedEvent
from mote.contracts.file.identity import FileChangeAttribution, FileChangeKind
from mote.contracts.ports.conversation.turn_context import TurnContextPriority
from mote.runtime.file_paths import display_path


class ChangedFilesContextSource:
    """Render each exact external version transition once."""

    name = "changed_files"
    telemetry_observer = True
    priority = TurnContextPriority.CHANGED_FILES
    save_to_context = False

    def __init__(self) -> None:
        self._pending: dict[str, FileChangedEvent] = {}

    async def handle(self, event: object) -> None:
        if isinstance(event, FileChangedEvent) and event.attribution is FileChangeAttribution.EXTERNAL:
            self._pending[event.path] = event

    async def render(self, *, cwd: Optional[str] = None) -> Optional[str]:
        if not self._pending:
            return None
        pending = tuple(self._pending.values())
        self._pending.clear()
        lines = [
            "# Files changed on disk",
            "These files changed outside your edits; any earlier view is stale. "
            "Re-read before relying on their contents:",
            "",
        ]
        for event in pending:
            suffix = " (deleted)" if event.change_type is FileChangeKind.DELETED else ""
            lines.append(f"- {display_path(event.path, cwd)}{suffix}")
        return "\n".join(lines)


__all__ = ["ChangedFilesContextSource"]
