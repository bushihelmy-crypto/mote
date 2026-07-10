"""CodeMapContextSource — a local structure map of the touched files, per turn.

The passive *navigation layer* distilled from CodeGraph. Where grep/read/glob
are the agent's *query-driven* tools (it must decide to reach for them), this
source *pushes* a small structural picture of the files the session has already
worked with — for each: the symbols it defines, which other touched files it
imports, and which import it. The map never carries source bodies (that stays a
Read away); it is a table of contents the model can use to target grep/read
instead of re-scanning files it has already opened.

Locality-scoped by construction: it only ever reflects the ``record_file_read``
trajectory (the touched set), never the whole repo. Cold-start exploration of an
unfamiliar tree is *not* its job — that still wants the query-driven tools. This
keeps the block small and the parse cost bounded to files the agent chose to
open.

Incremental (mirrors :class:`ChangedFilesContextSource`): each file's map row is
emitted once per distinct shape, keyed by a signature of its neighborhood
(defined symbols + within-set imports + importers). A file re-surfaces only when
its structure or its within-set edges actually change — editing a file, or a
newly-touched sibling importing it — so the reminder does not re-print an
unchanged map every turn.

Push→pull bridge in one object (like :class:`ToolCatalogContextSource`):
- as an :class:`~metagpt.common.interface.ObservationSubscriber` it catches
  :class:`~metagpt.common.events.PostCompactEvent` and clears the frontier, so
  the turn after a compaction re-emits the full map (the earlier one was
  condensed away with the rest of pre-compaction history);
- as an :class:`~metagpt.common.interface.EphemeralContextSource` it renders the
  changed rows once per think() cycle.

Duck-typed for its one cross-layer input (mirrors
:class:`SkillActivationContextSource`): it holds a plain ``get_touched_files``
callable so this low ``context`` layer never imports the Role. The
:class:`~metagpt.context.code_map.CodeMap` it drives lives in the *same* layer,
so it is owned directly (no indirection needed).
"""

from __future__ import annotations

import os
from typing import Callable, Optional

from metagpt.common.events import PostCompactEvent
from metagpt.common.interface import ObservationSubscriber, TurnContextPriority
from metagpt.context.code_map import CodeMap, FileNeighborhood


class CodeMapContextSource(ObservationSubscriber):
    """Emits the touched-set structure map per turn, incrementally."""

    name = "code_map"
    # Right after the external-edit freshness warning: once the model knows which
    # files went stale, the structure map orients where things live before the
    # ambient background/skill hints.
    priority = TurnContextPriority.CODE_MAP
    save_to_context = True

    def __init__(
        self,
        get_touched_files: Callable[[], list],
        code_map: Optional[CodeMap] = None,
    ) -> None:
        self._get_touched_files = get_touched_files
        # Owned directly (same context layer). Long-lived: holds the extractor's
        # mtime cache + the SQLite store across turns, so re-parsing is lazy.
        self._map = code_map if code_map is not None else CodeMap()
        # path -> last-emitted neighborhood signature, so an unchanged map row
        # stays quiet until its structure or within-set edges actually change.
        self._reported: dict[str, str] = {}

    async def handle(self, event) -> None:
        """Reset the incremental frontier after a compaction (re-emit the full map).

        The prior map rows were persisted into history and condensed away by the
        compaction, so the model no longer has them; clearing ``_reported`` makes
        the next render re-emit every non-trivial file. All other events ignored.
        """
        if isinstance(event, PostCompactEvent):
            self._reported = {}
        return None

    async def render(self, *, cwd: Optional[str] = None) -> Optional[str]:
        touched = self._get_touched_files() if self._get_touched_files else []
        if not touched:
            return None

        try:
            neighborhoods = self._map.neighborhood(list(touched))
        except Exception:  # noqa: BLE001 — best-effort; never break a turn
            return None

        changed: list[FileNeighborhood] = []
        for nb in neighborhoods:
            if not (nb.symbols or nb.imports or nb.imported_by):
                continue  # nothing structural to say about this file
            sig = self._signature(nb)
            if self._reported.get(nb.path) == sig:
                continue  # already emitted this exact shape
            self._reported[nb.path] = sig
            changed.append(nb)

        if not changed:
            return None

        lines = [
            "# Code map",
            "Structure of files you're working with — what each defines and how "
            "they depend on each other (within this set). Use it to target "
            "grep/read instead of re-scanning:",
            "",
        ]
        for nb in changed:
            lines.extend(self._render_file(nb, cwd))
        return "\n".join(lines).rstrip()

    # -- helpers -------------------------------------------------------------

    def _render_file(self, nb: FileNeighborhood, cwd: Optional[str]) -> list[str]:
        """One file's block: header + defines / imports / used-by sublines."""
        out = [f"- {self._display(nb.path, cwd)}"]
        if nb.symbols:
            names = ", ".join(s.qualified_name for s in nb.symbols)
            out.append(f"    defines: {names}")
        if nb.imports:
            out.append(f"    imports: {', '.join(self._display(p, cwd) for p in nb.imports)}")
        if nb.imported_by:
            out.append(f"    used by: {', '.join(self._display(p, cwd) for p in nb.imported_by)}")
        return out

    @staticmethod
    def _signature(nb: FileNeighborhood) -> str:
        """A stable key of the neighborhood's shape (symbols + within-set edges).

        Changes iff the file's defined symbols, its within-set imports, or its
        importers change — exactly the events that should re-surface the row.
        """
        syms = ",".join(s.qualified_name for s in nb.symbols)
        imps = ",".join(nb.imports)  # already sorted by the facade
        used = ",".join(nb.imported_by)  # already sorted by the facade
        return f"{syms}|{imps}|{used}"

    @staticmethod
    def _display(path: str, cwd: Optional[str]) -> str:
        if cwd:
            try:
                return os.path.relpath(path, cwd)
            except ValueError:  # different drive on Windows
                return path
        return path


__all__ = ["CodeMapContextSource"]
