"""Product CodeMap prompt adapter for touched files, rendered per turn.

The passive *navigation layer* distilled from CodeGraph. Where Search/Read
are the agent's *query-driven* tools (it must decide to reach for them), this
source *pushes* a small structural picture of the files the session has already
worked with — for each: the symbols it defines, which other touched files it
imports, and which import it. The map never carries source bodies (that stays a
Read away); it is a table of contents the model can use to target Search/Read
instead of re-scanning files it has already opened.

Locality-scoped by construction: it only ever reflects File Operations' observed
snapshot trajectory (the touched set), never the whole repo. Cold-start exploration of an
unfamiliar tree is *not* its job — that still wants the query-driven tools. This
keeps the block small and the parse cost bounded to files the agent chose to
open.

Incremental (mirrors :class:`ChangedFilesContextSource`): each file's map row is
emitted once per distinct shape, keyed by a signature of its neighborhood
(defined symbols + within-set imports + importers). A file re-surfaces only when
its structure or its within-set edges actually change — editing a file, or a
newly-touched sibling importing it — so the reminder does not re-print an
unchanged map every turn.

After a durable model-context rebuild commits, the context domain invokes
``on_model_context_rebuilt`` directly and clears the frontier before the new
live view is exposed. The next turn therefore re-emits the full map without
depending on telemetry delivery.

Duck-typed for its one cross-layer input (mirrors
:class:`SkillActivationContextSource`): it holds a plain ``get_touched_files``
callable so this low ``context`` layer never imports the Role. The
:class:`~mote.runtime.code_map.CodeMap` it drives lives in the *same* layer,
so it is owned directly (no indirection needed).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Callable, Optional

from mote.contracts.events.conversation import MODEL_CONTEXT_REBUILT_EVENTS, ModelContextRebuiltEvent
from mote.contracts.file.identity import PresentVersion
from mote.contracts.ports.code_intelligence.code_map import CodeMapLspQueryPort, CodeMapQueryPort
from mote.contracts.ports.conversation.turn_context import TurnContextPriority
from mote.product.code_map.collection import collect_code_map_files
from mote.product.code_map.enrichment import (
    diff_symbol_shapes,
    fill_unread_from_index,
    fill_unread_symbols,
    neighborhood_has_content,
    neighborhood_signature,
    resolve_in_context,
    resolve_precise_callers,
    resolve_surfaced_callers,
)
from mote.product.code_map.rendering import render_code_map
from mote.runtime.code_map import CodeMap, FileNeighborhood

# When a file defines more than this many symbols, fold the tail behind a
# "(+N more)" summary so one large file cannot dominate the map block.
# Default token ceiling for the whole rendered block. Degrades in three tiers
# to fit (full → drop signatures/calls → names + edge counts only).
_DEFAULT_MAX_TOKENS = 1200


class CodeMapContextSource:
    """Emits the touched-set structure map per turn, incrementally."""

    name = "code_map"
    # Right after the external-edit freshness warning: once the model knows which
    # files went stale, the structure map orients where things live before the
    # ambient background/skill hints.
    priority = TurnContextPriority.CODE_MAP
    save_to_context = True

    def __init__(
        self,
        get_touched_files: Callable[[], list[str]],
        code_map: Optional[CodeMap] = None,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        lsp_query: CodeMapLspQueryPort | None = None,
        repo_index: CodeMapQueryPort | None = None,
        get_read_state: Callable[[], Mapping[str, PresentVersion]] | None = None,
        get_glimpsed_files: Callable[[], list[str]] | None = None,
        surface_callers: bool = False,
    ) -> None:
        self._get_touched_files = get_touched_files
        # P2: duck-typed provider of files surfaced by a Search match but not
        # read in full. Merged into the map's file set so a searched-but-unopened
        # file's structure (defines + intent) can guide "what should I read".
        # None -> the map reflects only the read trajectory.
        self._get_glimpsed_files = get_glimpsed_files
        # Owned directly (same context layer). Long-lived: holds the extractor's
        # mtime cache + the SQLite store across turns, so re-parsing is lazy.
        self._map = code_map if code_map is not None else CodeMap()
        self._max_tokens = max_tokens
        # Layer B: duck-typed async LSP facade (document_symbols / definition /
        # references). None -> dangling-import symbol resolution + precise callers
        # are silently off.
        self._lsp_query = lsp_query
        # Layer C: duck-typed whole-repo reverse-dep source (has ``importers``).
        # None -> ``used by:`` falls back to the touched-set-scoped query.
        self._repo_index = repo_index
        # F1: duck-typed ``{abspath: PresentVersion}`` provider (the same
        # seam ChangedFilesContextSource reads). None -> self-description always
        # renders.
        self._get_read_state = get_read_state
        # P3: opportunistic symbol-level callers. When on (and an LSP facade is
        # wired), a *calm* row's public top-level symbols get their real call
        # sites queried once per version — rendered as ``foo called by: a.py`` so
        # the model sees the reverse call direction, not just file-level "used by".
        # Default off: it adds LSP ``references`` volume, so it is opt-in until the
        # cheaper F1/F2/F3 signals are shown to be insufficient (see the plan).
        self._surface_callers = surface_callers
        # path -> last-emitted neighborhood signature, so an unchanged map row
        # stays quiet until its structure or within-set edges actually change.
        self._reported: dict[str, str] = {}
        # F1: paths whose exact observed version remains live in File Operations.
        # Their ``defines:``/``calls:`` self-description is
        # suppressed — it only restates what's already in context. Recomputed each
        # render from ``get_read_state``; a compaction condenses the bodies away, so
        # the very next render skips the recompute (``_post_compact``) to re-show
        # every file's defines once before the read-state-derived frontier resumes.
        self._in_context: set[str] = set()
        self._post_compact = False
        # F3: path -> {qualified_name: signature} of the *last* extract, so a fresh
        # extract can be diffed for an interface change (removed symbol / changed
        # signature) that breaks unseen callers. Cleared on model-context rebuild.
        self._shape_cache: dict[str, dict[str, str]] = {}
        # F3 per-render (recomputed each render, folded into the signature):
        #   _changed_names: path -> [broken qualified names] (removed OR sig-changed)
        #                   — drives the risk label + re-surface fold.
        #   _changed_symbols: path -> [Symbol] still existing (sig-changed only)
        #                   — F2's references targets (a removed symbol can't be queried).
        #   _precise: path -> {qualified_name: [caller display paths]} from F2.
        #   _surfaced: path -> {qualified_name: [caller display paths]} from P3 —
        #             opportunistic callers of calm public symbols (kept separate
        #             from _precise so the interface-change ⚠ path is unchanged).
        self._changed_names: dict[str, list[str]] = {}
        self._changed_symbols: dict[str, list] = {}
        self._precise: dict[str, dict[str, list[str]]] = {}
        self._surfaced: dict[str, dict[str, list[str]]] = {}

    async def on_model_context_rebuilt(self, event: ModelContextRebuiltEvent) -> None:
        """Reset the incremental frontier when stored history is structurally rebuilt.

        A compaction condenses the prior map rows away; a ``/clear`` or user
        delete prunes the messages that carried them. Both fold to
        ``MODEL_CONTEXT_REBUILT_EVENTS`` — the model no longer has the rows, so clearing
        ``_reported`` makes the next render re-emit every non-trivial file. The
        in-context frontier is also cleared (bodies were condensed/pruned away →
        show ``defines:`` again) and the symbol-shape baseline reset (a fresh
        baseline, so the first render after does not mis-fire the interface-change
        risk label). All other events ignored.
        """
        if isinstance(event, MODEL_CONTEXT_REBUILT_EVENTS):
            self._reported = {}
            self._in_context = set()
            self._post_compact = True
            self._shape_cache = {}

    async def render(self, *, cwd: Optional[str] = None) -> Optional[str]:
        files = collect_code_map_files(
            self._get_touched_files,
            self._get_glimpsed_files,
        )
        if not files:
            return None

        # Layer C: whole-repo reverse deps when a repo index is wired; else the
        # touched-set-scoped query.
        repo_index = self._repo_index
        repo_importers = (lambda candidates: list(repo_index.importers(candidates))) if repo_index is not None else None
        try:
            neighborhoods = self._map.neighborhood(files, repo_importers=repo_importers)
        except Exception:  # noqa: BLE001 — best-effort; never break a turn
            return None

        # Baseline: resolve dangling-import symbols / purpose / used-by from the
        # persistent whole-repo index (LSP-free). Runs first so LSP (below) only
        # *overrides* symbols where it has a more precise answer.
        if self._repo_index is not None:
            fill_unread_from_index(neighborhoods, self._map, self._repo_index)

        # Layer B: refine dangling-import symbols for the changed rows via LSP —
        # merges over the index baseline (LSP wins per target).
        if self._lsp_query is not None:
            await fill_unread_symbols(neighborhoods, self._map, self._lsp_query)

        # F1: refresh the in-context frontier (bodies live in history) from the
        # read-state map before gating self-description.
        self._in_context, self._post_compact = resolve_in_context(
            neighborhoods,
            self._get_read_state,
            post_compact=self._post_compact,
        )
        # F3: diff each row's symbol shape against the cached one to flag an
        # interface change (a removed/renamed symbol or a changed signature). The
        # per-path result feeds the risk label + F2's precise-caller targets.
        self._changed_names, self._changed_symbols = diff_symbol_shapes(
            neighborhoods,
            self._shape_cache,
        )
        # F2: for interface-changed rows, name the exact call sites via LSP
        # references (bounded, cached per version). Falls back to string-index
        # ``used by:`` when LSP is off or returns nothing.
        self._precise = {}
        if self._lsp_query is not None:
            self._precise = await resolve_precise_callers(
                neighborhoods,
                self._changed_symbols,
                self._map,
                self._lsp_query,
            )

        # P3: opportunistic callers for calm rows' public symbols (opt-in, LSP-gated).
        self._surfaced = {}
        if self._surface_callers and self._lsp_query is not None:
            self._surfaced = await resolve_surfaced_callers(
                neighborhoods,
                self._changed_names,
                self._map,
                self._lsp_query,
            )

        changed: list[FileNeighborhood] = []
        for nb in neighborhoods:
            if not neighborhood_has_content(
                nb,
                self._changed_names,
                self._in_context,
            ):
                continue  # nothing to say about this file (edges + self-desc empty)
            sig = neighborhood_signature(
                nb,
                in_context=self._in_context,
                changed_names=self._changed_names,
                precise_callers=self._precise,
                surfaced_callers=self._surfaced,
            )
            if self._reported.get(nb.path) == sig:
                continue  # already emitted this exact shape
            changed.append(nb)

        if not changed:
            return None

        block = render_code_map(
            changed,
            cwd,
            self._max_tokens,
            in_context=self._in_context,
            changed_names=self._changed_names,
            precise_callers=self._precise,
            surfaced_callers=self._surfaced,
        )
        # Only commit the frontier once we've actually emitted the rows (a render
        # that produced nothing must not mark them reported).
        for nb in changed:
            self._reported[nb.path] = neighborhood_signature(
                nb,
                in_context=self._in_context,
                changed_names=self._changed_names,
                precise_callers=self._precise,
                surfaced_callers=self._surfaced,
            )
        return block


__all__ = ["CodeMapContextSource"]
