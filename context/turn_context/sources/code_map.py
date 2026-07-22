"""CodeMapContextSource — a local structure map of the touched files, per turn.

The passive *navigation layer* distilled from CodeGraph. Where Search/Read
are the agent's *query-driven* tools (it must decide to reach for them), this
source *pushes* a small structural picture of the files the session has already
worked with — for each: the symbols it defines, which other touched files it
imports, and which import it. The map never carries source bodies (that stays a
Read away); it is a table of contents the model can use to target Search/Read
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
- as an :class:`~mote.common.interface.ObservationSubscriber` it catches any
  :data:`~mote.common.events.HISTORY_RESET_EVENTS` (a compaction *or* a ``/clear`` /
  user delete that structurally rebuilds stored history) and clears the frontier,
  so the next turn re-emits the full map (the earlier one was condensed away by
  the compaction or pruned by the edit);
- as an :class:`~mote.common.interface.EphemeralContextSource` it renders the
  changed rows once per think() cycle.

Duck-typed for its one cross-layer input (mirrors
:class:`SkillActivationContextSource`): it holds a plain ``get_touched_files``
callable so this low ``context`` layer never imports the Role. The
:class:`~mote.context.code_map.CodeMap` it drives lives in the *same* layer,
so it is owned directly (no indirection needed).
"""

from __future__ import annotations

from typing import Callable, Optional

from mote.common.disk import mtime_ns
from mote.common.events import HISTORY_RESET_EVENTS
from mote.common.interface import ObservationSubscriber, TurnContextPriority
from mote.common.text import display_path
from mote.common.utils.prompt_sanitizer import count_tokens
from mote.context.code_map import CodeMap, FileNeighborhood

# When a file defines more than this many symbols, fold the tail behind a
# "(+N more)" summary so one large file cannot dominate the map block.
_MAX_SYMBOLS_PER_FILE = 12
# Cap on whole-repo importers shown per unread dependency (Opt A), so a widely-
# imported dangling target's "used by:" cannot dominate the block.
_UNREAD_USEDBY_CAP = 8
# Default token ceiling for the whole rendered block. Degrades in three tiers
# to fit (full → drop signatures/calls → names + edge counts only).
_DEFAULT_MAX_TOKENS = 1200


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
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        lsp_query: Optional[object] = None,
        repo_index: Optional[object] = None,
        get_read_state: Optional[Callable[[], dict]] = None,
        get_glimpsed_files: Optional[Callable[[], list]] = None,
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
        # F1: duck-typed ``{abspath: mtime_ns_when_last_read}`` provider (the same
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
        # F1: paths whose body we believe is live in history (read at the current
        # on-disk version). Their ``defines:``/``calls:`` self-description is
        # suppressed — it only restates what's already in context. Recomputed each
        # render from ``get_read_state``; a compaction condenses the bodies away, so
        # the very next render skips the recompute (``_post_compact``) to re-show
        # every file's defines once before the read-state-derived frontier resumes.
        self._in_context: set[str] = set()
        self._post_compact = False
        # F3: path -> {qualified_name: signature} of the *last* extract, so a fresh
        # extract can be diffed for an interface change (removed symbol / changed
        # signature) that breaks unseen callers. Cleared on any history reset.
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

    async def handle(self, event) -> None:
        """Reset the incremental frontier when stored history is structurally rebuilt.

        A compaction condenses the prior map rows away; a ``/clear`` or user
        delete prunes the messages that carried them. Both fold to
        ``HISTORY_RESET_EVENTS`` — the model no longer has the rows, so clearing
        ``_reported`` makes the next render re-emit every non-trivial file. The
        in-context frontier is also cleared (bodies were condensed/pruned away →
        show ``defines:`` again) and the symbol-shape baseline reset (a fresh
        baseline, so the first render after does not mis-fire the interface-change
        risk label). All other events ignored.
        """
        if isinstance(event, HISTORY_RESET_EVENTS):
            self._reported = {}
            self._in_context = set()
            self._post_compact = True
            self._shape_cache = {}
        return None

    def _collect_files(self) -> list:
        """The map's file set: the read trajectory plus glimpsed search hits (P2).

        Read files come first (the working set), then glimpsed-only files a
        Search surfaced but the model has not opened — deduped, order-preserved
        so an already-read file is never demoted to a glimpse. Best-effort: a
        raising provider contributes nothing.
        """
        try:
            touched = list(self._get_touched_files()) if self._get_touched_files else []
        except Exception:  # noqa: BLE001 — advisory; never break a turn
            touched = []
        try:
            glimpsed = list(self._get_glimpsed_files()) if self._get_glimpsed_files else []
        except Exception:  # noqa: BLE001
            glimpsed = []
        seen = set(touched)
        return touched + [p for p in glimpsed if p not in seen]

    async def render(self, *, cwd: Optional[str] = None) -> Optional[str]:
        files = self._collect_files()
        if not files:
            return None

        # Layer C: whole-repo reverse deps when a repo index is wired; else the
        # touched-set-scoped query.
        repo_importers = getattr(self._repo_index, "importers", None) if self._repo_index else None
        try:
            neighborhoods = self._map.neighborhood(files, repo_importers=repo_importers)
        except Exception:  # noqa: BLE001 — best-effort; never break a turn
            return None

        # Baseline: resolve dangling-import symbols / purpose / used-by from the
        # persistent whole-repo index (LSP-free). Runs first so LSP (below) only
        # *overrides* symbols where it has a more precise answer.
        if self._repo_index is not None:
            self._fill_unread_from_index(neighborhoods)

        # Layer B: refine dangling-import symbols for the changed rows via LSP —
        # merges over the index baseline (LSP wins per target).
        if self._lsp_query is not None:
            await self._fill_unread_symbols(neighborhoods)

        # F1: refresh the in-context frontier (bodies live in history) from the
        # read-state map before gating self-description.
        self._refresh_in_context(neighborhoods)
        # F3: diff each row's symbol shape against the cached one to flag an
        # interface change (a removed/renamed symbol or a changed signature). The
        # per-path result feeds the risk label + F2's precise-caller targets.
        self._changed_symbols = self._diff_shapes(neighborhoods)
        # F2: for interface-changed rows, name the exact call sites via LSP
        # references (bounded, cached per version). Falls back to string-index
        # ``used by:`` when LSP is off or returns nothing.
        self._precise = {}
        if self._lsp_query is not None:
            await self._fill_precise_callers(neighborhoods)

        # P3: opportunistic callers for calm rows' public symbols (opt-in, LSP-gated).
        self._surfaced = {}
        if self._surface_callers and self._lsp_query is not None:
            await self._fill_surfaced_callers(neighborhoods)

        changed: list[FileNeighborhood] = []
        for nb in neighborhoods:
            if not self._has_content(nb):
                continue  # nothing to say about this file (edges + self-desc empty)
            sig = self._signature(nb)
            if self._reported.get(nb.path) == sig:
                continue  # already emitted this exact shape
            changed.append(nb)

        if not changed:
            return None

        block = self._render_within_budget(changed, cwd)
        # Only commit the frontier once we've actually emitted the rows (a render
        # that produced nothing must not mark them reported).
        for nb in changed:
            self._reported[nb.path] = self._signature(nb)
        return block

    def _has_content(self, nb: FileNeighborhood) -> bool:
        """True when the row would render at least one line.

        Edges (imports / unread / used-by) are always shown; the ``defines:`` /
        ``calls:`` self-description is shown only when the body is NOT in context
        (F1). So an in-context file with no edges has nothing left to say — drop it
        (the same "nothing structural" guard, now read-state-aware). An
        interface-changed file always has content (its ⚠ risk label), even a pure
        deletion that left it structureless.
        """
        if nb.path in self._changed_names:
            return True  # the ⚠ risk label always renders
        if nb.imports or nb.imported_by or nb.imports_unread:
            return True
        if nb.path in self._in_context:
            return False  # body in history, no edges → suppressed row is empty
        return bool(nb.symbols or nb.calls)

    def _fill_unread_from_index(self, neighborhoods: list) -> None:
        """LSP-free baseline: resolve unread targets from the whole-repo index.

        For every row with dangling imports, ask the persistent index (via the
        duck-typed readers on ``self._repo_index``) what each untouched target
        *defines*, its *purpose* (Opt B), and who else *depends on* it (Opt A).
        This is the baseline any session gets without an LSP; the LSP pass below
        only overrides the symbol view where it can. Best-effort throughout — a
        missing reader or a raise leaves the fields empty (bare path renders).
        """
        symbols_of = getattr(self._repo_index, "symbols_in", None)
        module_summary_of = getattr(self._repo_index, "module_summary_of", None)
        importers_of = getattr(self._repo_index, "importers", None)
        if symbols_of is None or module_summary_of is None or importers_of is None:
            return
        # Decision B: prefer the symbol-precise whole-repo reverse-dep query when
        # the index exposes it, so an unread target's ``used by:`` names who uses
        # its API, not merely who imports its module. None -> module-level query.
        references_of = getattr(self._repo_index, "references_to", None)
        for nb in neighborhoods:
            if not nb.imports_unread:
                continue
            try:
                syms, summaries, used_by = self._map.resolve_unread_from_index(
                    nb.imports_unread,
                    symbols_of=symbols_of,
                    module_summary_of=module_summary_of,
                    importers_of=importers_of,
                    references_of=references_of,
                )
                nb.unread_symbols = syms
                nb.unread_module_summary = summaries
                nb.unread_imported_by = used_by
            except Exception:  # noqa: BLE001 — never break a turn on a bad resolve
                pass

    async def _fill_unread_symbols(self, neighborhoods: list) -> None:
        """Refine dangling-import target symbols via LSP — overlay on the index baseline.

        Keeps the LSP-free index baseline (:meth:`_fill_unread_from_index`) and
        merges the LSP-resolved symbols over it per target, so LSP (more precise)
        wins where it answers but the index insight stands where it does not.
        Purpose / used-by stay index-sourced (LSP resolves symbols only). Best-
        effort: a raising resolver leaves the baseline untouched. Only rows that
        actually have unread imports are queried.
        """
        for nb in neighborhoods:
            if not nb.imports_unread:
                continue
            try:
                lsp_syms = await self._map.resolve_unread(nb.path, nb.imports_unread, self._lsp_query)
            except Exception:  # noqa: BLE001 — never break a turn on a bad resolve
                lsp_syms = {}
            nb.unread_symbols = {**nb.unread_symbols, **lsp_syms}

    # -- F1: read-state / in-context frontier --------------------------------

    def _refresh_in_context(self, neighborhoods: list) -> None:
        """Recompute which touched files' bodies are live in history this turn.

        A file counts as *in context* when the read-state records it was read at
        its *current* on-disk mtime — the body the model saw is still accurate, so
        re-stating its ``defines:``/``calls:`` only wastes attention. It is *not*
        in context when there is no read entry (surfaced purely as a dependency) or
        the recorded mtime is stale (edited since last read — Feature 3 wants to
        re-show it *and* flag the interface risk). With no ``get_read_state`` the
        frontier stays empty → self-description always renders.
        """
        if self._get_read_state is None:
            return
        # One render after a compaction the bodies were condensed away, so nothing
        # is truly in context regardless of read-state mtimes — show every file's
        # defines once, then let the read-state-derived frontier resume next turn.
        if self._post_compact:
            self._post_compact = False
            self._in_context = set()
            return
        try:
            read_state = self._get_read_state() or {}
        except Exception:  # noqa: BLE001 — advisory; never break a turn
            read_state = {}
        in_context: set[str] = set()
        for nb in neighborhoods:
            recorded = read_state.get(nb.path)
            if recorded is None:
                continue  # never read (dependency-only) → show defines
            if mtime_ns(nb.path) == recorded:
                in_context.add(nb.path)  # read at this version → body in history
        self._in_context = in_context

    # -- F3: symbol-shape diff (interface change) ----------------------------

    def _diff_shapes(self, neighborhoods: list) -> dict[str, list]:
        """Flag rows whose public interface changed; return their queryable symbols.

        Diffs each row's fresh ``{qualified_name: signature}`` shape against the
        cached one from the previous render. A *breaking* change is a name removed
        or a retained name whose signature changed — exactly what breaks an unseen
        caller. Names added only are non-breaking and do NOT trigger the risk
        label. The shape cache is updated after diffing.

        Populates ``self._changed_names`` (``{path: [broken qualified names]}`` —
        drives the risk label, includes removals) and returns
        ``{path: [Symbol]}`` of the *still-existing* breaking symbols (F2's
        references targets — a removed symbol has no def site left to query).
        """
        self._changed_names = {}
        changed: dict[str, list] = {}
        for nb in neighborhoods:
            fresh = {s.qualified_name: s.signature for s in nb.symbols}
            prior = self._shape_cache.get(nb.path)
            # Update the baseline regardless (so the next diff compares against
            # this turn's shape). A first-sight file has no prior → no risk fired.
            self._shape_cache[nb.path] = fresh
            if not prior:
                continue
            broken_names = [
                name
                for name in prior
                if self._is_public_interface(name) and (name not in fresh or fresh[name] != prior[name])
            ]
            if not broken_names:
                continue
            self._changed_names[nb.path] = broken_names
            # F2 can only query symbols that still exist (a retained, sig-changed
            # def). A pure deletion trips the label but has no def site to query.
            broken_set = set(broken_names)
            changed[nb.path] = [s for s in nb.symbols if s.qualified_name in broken_set]
        return changed

    @staticmethod
    def _is_public_interface(qualified_name: str) -> bool:
        """True for a *public top-level* symbol — the only kind a caller imports.

        The risk label exists to flag breakage of a file's *public surface*: what
        an unseen caller could ``import``. That is a top-level def/class whose name
        is public. So we filter out:

        - **nested** symbols (a dotted ``qualified_name`` — a method or closure);
          they are reached through their owner, not imported directly, and a
          method-signature churn is not a module-level contract break.
        - **private** symbols (a leading ``_`` on the top-level name); by
          convention these are not part of the importable interface.
        """
        if "." in qualified_name:
            return False  # nested (method / closure), not a module-level export
        return not qualified_name.startswith("_")

    # -- F2: on-demand precise callers ---------------------------------------

    async def _fill_precise_callers(self, neighborhoods: list) -> None:
        """Name exact call sites for interface-changed rows via LSP references.

        Only rows flagged by :meth:`_diff_shapes` are queried, and only their
        broken symbols — never a per-turn full-workspace scan. Best-effort: a
        raising facade leaves the row with just its string-index ``used by:``.
        """
        by_path = {nb.path: nb for nb in neighborhoods}
        for path, symbols in self._changed_symbols.items():
            nb = by_path.get(path)
            if nb is None or not symbols:
                continue
            try:
                self._precise[path] = await self._map.precise_callers(path, symbols, self._lsp_query)
            except Exception:  # noqa: BLE001 — never break a turn on a bad query
                self._precise[path] = {}

    # -- P3: opportunistic callers of calm public symbols --------------------

    async def _fill_surfaced_callers(self, neighborhoods: list) -> None:
        """Query real callers for each row's *public top-level* symbols (opt-in).

        Where F2 fires only when a symbol's interface breaks, P3 surfaces the
        reverse call direction for *calm* rows too — so the model sees "who calls
        this" without an edit. Only public top-level symbols are queried (a caller
        imports those, not methods/privates — same filter as the risk label), and
        an interface-changed row is skipped here (F2 already owns its callers, and
        its ⚠ block renders them). The same :meth:`CodeMap.precise_callers` does
        the work — bounded by ``_MAX_REF_SYMBOLS`` and cached per version — so the
        query cost matches F2's; this only widens *when* it runs, behind the flag.
        """
        for nb in neighborhoods:
            if nb.path in self._changed_names:
                continue  # F2 owns this row's callers (rendered under the ⚠ label)
            targets = [s for s in nb.symbols if self._is_public_interface(s.qualified_name)]
            if not targets:
                continue
            try:
                self._surfaced[nb.path] = await self._map.precise_callers(nb.path, targets, self._lsp_query)
            except Exception:  # noqa: BLE001 — never break a turn on a bad query
                self._surfaced[nb.path] = {}

    # -- rendering -----------------------------------------------------------

    def _render_within_budget(self, changed: list[FileNeighborhood], cwd: Optional[str]) -> str:
        """Render the changed rows, degrading detail across three tiers to fit.

        Tier 0: full (defines+signatures, calls, imports, unread, used-by).
        Tier 1: drop signatures and the calls detail (structure only).
        Tier 2: name + edge *counts* only (the floor — always emitted even if it
        overflows, since a bare index still orients better than nothing).
        """
        for tier in (0, 1):
            block = self._compose(changed, cwd, tier)
            if count_tokens(block) <= self._max_tokens:
                return block
        return self._compose(changed, cwd, 2)

    def _compose(self, changed: list[FileNeighborhood], cwd: Optional[str], tier: int) -> str:
        lines = [
            "# Code map",
            "Structure of files you're working with — what each defines and how "
            "they depend on each other (within this set). Use it to target "
            "Search/Read instead of re-scanning:",
            "",
        ]
        for nb in changed:
            lines.extend(self._render_file(nb, cwd, tier))
        return "\n".join(lines).rstrip()

    def _render_file(self, nb: FileNeighborhood, cwd: Optional[str], tier: int) -> list[str]:
        """One file's block, at the given detail *tier*.

        F1: the ``defines:`` / ``calls:`` self-description is suppressed when the
        file's body is in context (already in history) — it would only restate what
        the model can already see. The *edge* sub-lines (imports / unread / used-by)
        are never in the body, so they always render.

        F3/F2: an interface-changed file gets a prominent ``⚠ interface changed``
        label listing the broken symbols, and its ``used by:`` is rendered under
        that label (never dropped by tier), preferring the LSP-resolved precise
        callers over the string-index whole-repo list.
        """
        interface_changed = nb.path in self._changed_names
        out = [f"- {display_path(nb.path, cwd)}"]
        in_context = nb.path in self._in_context
        # P1: the module's one-line intent, so the model knows what the file is
        # for without opening it. Tier 0 only (dropped first under budget
        # pressure) and never for an in-context file (its body is already shown).
        if tier < 1 and nb.module_summary and (not in_context or interface_changed):
            out.append(f"    purpose: {nb.module_summary}")
        # F1: gate self-description on body-not-in-context. An interface change
        # re-shows defines regardless — the model needs to see what it just broke.
        if nb.symbols and (not in_context or interface_changed):
            out.extend(self._render_defines(nb.symbols, tier))
            if tier < 1:
                calls = self._render_calls(nb.calls)
                if calls:
                    out.append(f"    calls: {calls}")
        # P3: opportunistic per-symbol callers for calm rows (tier 0 only, so it
        # drops first under budget pressure). Interface-changed rows render their
        # callers under the ⚠ label instead (see _render_risk), so skip them here.
        if tier < 1 and not interface_changed:
            out.extend(self._render_surfaced_callers(nb, cwd))
        if nb.imports:
            targets = ", ".join(display_path(p, cwd) for p in nb.imports)
            out.append(f"    imports: {targets}")
        if nb.imports_unread:
            unread = ", ".join(
                self._render_unread(p, nb.unread_symbols, nb.unread_module_summary, cwd, tier)
                for p in nb.imports_unread
            )
            out.append(f"    also imports (unread): {unread}")
            # Opt A: whole-repo reverse-deps of each unread target (tier 0 only —
            # drops first under budget pressure). Shows who else in the repo
            # depends on a file the agent has not opened, capped so a hub file's
            # importer list stays bounded.
            if tier < 1:
                out.extend(self._render_unread_used_by(nb, cwd))
        if interface_changed:
            out.extend(self._render_risk(nb, cwd))
        elif nb.imported_by:
            out.append(f"    used by: {', '.join(display_path(p, cwd) for p in nb.imported_by)}")
        return out

    def _render_risk(self, nb: FileNeighborhood, cwd: Optional[str]) -> list[str]:
        """The ⚠ interface-changed label + callers (exempt from tier degradation).

        Names the broken symbols so the model sees *what* changed, then the exact
        call sites: F2's LSP-resolved per-symbol callers when available (``foo()
        called by: a.py, b.py``), falling back to the string-index whole-repo
        ``used by:`` when LSP returned nothing. Always emitted — this is the one
        thing the token budget must not drop, since a broken caller is precisely
        the risk ``used by:`` exists to surface.
        """
        broken = ", ".join(self._changed_names.get(nb.path, []))
        out = [f"    ⚠ interface changed: {broken}"]
        precise = self._precise.get(nb.path) or {}
        rendered_precise = False
        for sym in self._changed_names.get(nb.path, []):
            callers = precise.get(sym)
            if callers:
                names = ", ".join(display_path(p, cwd) for p in callers)
                out.append(f"      {sym} called by: {names}")
                rendered_precise = True
        if not rendered_precise and nb.imported_by:
            out.append(f"    used by: {', '.join(display_path(p, cwd) for p in nb.imported_by)}")
        return out

    def _render_surfaced_callers(self, nb: FileNeighborhood, cwd: Optional[str]) -> list[str]:
        """P3 ``sym called by: a.py, b.py`` lines for a calm row's public symbols.

        Renders the opportunistic callers gathered by :meth:`_fill_surfaced_callers`
        — the reverse call direction the model otherwise only gets on an interface
        break. Empty when the flag is off, LSP is unwired, or nothing resolved.
        """
        surfaced = self._surfaced.get(nb.path) or {}
        out: list[str] = []
        for sym in sorted(surfaced):
            callers = surfaced.get(sym)
            if callers:
                names = ", ".join(display_path(p, cwd) for p in callers)
                out.append(f"    {sym} called by: {names}")
        return out

    def _render_unread(
        self, path: str, unread_symbols: dict, unread_module_summary: dict, cwd: Optional[str], tier: int
    ) -> str:
        """One unread-import target: ``pkg/other.py (thing, helper) — <purpose>``.

        At tier 0 the resolved symbols ``(names)`` and the module *purpose* (Opt
        B) ride alongside the path. At tier ≥1 both annotations are dropped to
        respect the token budget — the bare path still orients the model to the
        dependency.
        """
        display = display_path(path, cwd)
        if tier >= 1:
            return display
        names = unread_symbols.get(path) if unread_symbols else None
        if names:
            display = f"{display} ({', '.join(names)})"
        summary = unread_module_summary.get(path) if unread_module_summary else None
        if summary:
            display = f"{display} — {summary}"
        return display

    def _render_unread_used_by(self, nb: FileNeighborhood, cwd: Optional[str]) -> list[str]:
        """Opt A ``<unread.py> used by: a.py, b.py (+N more)`` lines (tier 0).

        Per unread dependency target that has whole-repo reverse-deps, a nested
        line naming its importers, capped at :data:`_UNREAD_USEDBY_CAP`. Empty
        when no target resolved any importers (index off / none found).
        """
        out: list[str] = []
        for target in nb.imports_unread:
            importers = nb.unread_imported_by.get(target) if nb.unread_imported_by else None
            if not importers:
                continue
            head = importers[:_UNREAD_USEDBY_CAP]
            names = ", ".join(display_path(p, cwd) for p in head)
            overflow = len(importers) - len(head)
            if overflow > 0:
                names = f"{names} (+{overflow} more)"
            out.append(f"      {display_path(target, cwd)} used by: {names}")
        return out

    def _render_defines(self, symbols: list, tier: int) -> list[str]:
        """The ``defines:`` sub-lines for a file, honouring the tier's verbosity.

        Tier 0 with any documented symbol expands to one line per symbol —
        ``name(sig) — summary`` (P1) — so the model reads each symbol's intent
        without opening the file; undocumented symbols render as bare
        ``name(sig)``. Tier 0 with no summaries, and every tier ≥1, collapses to
        the compact single ``defines: a, b, c`` line (names + signatures at tier
        0, bare names above). Either way the tail past :data:`_MAX_SYMBOLS_PER_FILE`
        folds to ``(+N more)`` so one big file stays bounded.
        """
        head = symbols[:_MAX_SYMBOLS_PER_FILE]
        overflow = len(symbols) - len(head)
        if tier < 1 and any(s.summary for s in head):
            out = ["    defines:"]
            for s in head:
                sig = s.signature or ""
                label = f"{s.qualified_name}{sig}"
                out.append(f"      {label} — {s.summary}" if s.summary else f"      {label}")
            if overflow > 0:
                out.append(f"      (+{overflow} more)")
            return out
        return [f"    defines: {self._render_symbols(head, overflow, tier)}"]

    @staticmethod
    def _render_symbols(head: list, overflow: int, tier: int) -> str:
        """Compact single-line symbol list (names + signatures at tier 0).

        The no-summary / degraded-tier form: ``a(sig), b(sig), (+N more)`` at
        tier 0, bare qualified names above. *head* is already capped by the
        caller and *overflow* is the folded remainder.
        """
        if tier < 1:
            names = [f"{s.qualified_name}{s.signature}" if s.signature else s.qualified_name for s in head]
        else:
            names = [s.qualified_name for s in head]
        rendered = ", ".join(names)
        if overflow > 0:
            rendered = f"{rendered}, (+{overflow} more)"
        return rendered

    @staticmethod
    def _render_calls(calls: list) -> str:
        """Aggregate ``caller -> {callees}`` from the (possibly repeated) edges.

        The extractor may emit an edge more than once (a callee invoked on
        several lines); we collapse to unique caller→callee arrows, module-level
        calls shown from ``<module>``.
        """
        by_caller: dict[str, list[str]] = {}
        seen: set = set()
        for c in calls:
            key = (c.caller, c.callee)
            if key in seen:
                continue
            seen.add(key)
            caller = c.caller or "<module>"
            by_caller.setdefault(caller, []).append(c.callee)
        parts = [f"{caller} → {', '.join(callees)}" for caller, callees in by_caller.items()]
        return "; ".join(parts)

    # -- helpers -------------------------------------------------------------

    def _signature(self, nb: FileNeighborhood) -> str:
        """A stable key of the neighborhood's shape (symbols + all edges + risk).

        Changes iff the file's defined symbols, its within-set imports, its
        dangling internal imports, its intra-file calls, or its importers change
        — exactly the events that should re-surface the row. Also folds:

        - F1 in-context bit: a file transitioning in→out of context (e.g. after a
          compaction) re-surfaces its ``defines:``;
        - F3 interface-change: the risky row re-surfaces the turn the change
          happens even if the raw edge set is unchanged;
        - F2 precise callers: a change in resolved callers re-surfaces the row
          (mirrors the Layer B ``resolved`` fold);
        - P3 surfaced callers: a change in a calm public symbol's resolved callers
          re-surfaces the row so a newly-appearing "called by" is not swallowed.
        """
        # P1: fold the module + per-symbol summaries so a docstring edit that
        # changes the rendered intent re-surfaces the row (the raw symbol/edge
        # sets may be otherwise unchanged).
        syms = ",".join(f"{s.qualified_name}:{s.summary}" for s in nb.symbols)
        mod = nb.module_summary
        imps = ",".join(nb.imports)  # already sorted by the facade
        unread = ",".join(nb.imports_unread)  # already sorted by the facade
        used = ",".join(nb.imported_by)  # already sorted by the facade
        calls = ",".join(sorted({f"{c.caller}>{c.callee}" for c in nb.calls}))
        # Fold resolved unread symbols so a newly-resolved "defines" view
        # re-surfaces the row (Layer B). Sorted by target for a stable key.
        resolved = ";".join(f"{p}:{','.join(nb.unread_symbols[p])}" for p in sorted(nb.unread_symbols or {}))
        # Opt B/A: fold the unread targets' purpose + whole-repo used-by so a
        # newly-resolved purpose or a changed importer set re-surfaces the row.
        unread_purpose = ";".join(f"{p}:{nb.unread_module_summary[p]}" for p in sorted(nb.unread_module_summary or {}))
        unread_used = ";".join(f"{p}:{','.join(nb.unread_imported_by[p])}" for p in sorted(nb.unread_imported_by or {}))
        in_ctx = "1" if nb.path in self._in_context else "0"
        risk = ",".join(self._changed_names.get(nb.path, []))
        precise = ";".join(
            f"{sym}:{','.join(callers)}" for sym, callers in sorted((self._precise.get(nb.path) or {}).items())
        )
        surfaced = ";".join(
            f"{sym}:{','.join(callers)}" for sym, callers in sorted((self._surfaced.get(nb.path) or {}).items())
        )
        return (
            f"{mod}|{syms}|{imps}|{unread}|{used}|{calls}|{resolved}|"
            f"{unread_purpose}|{unread_used}|{in_ctx}|{risk}|{precise}|{surfaced}"
        )


__all__ = ["CodeMapContextSource"]
