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
- as an :class:`~mote.common.interface.ObservationSubscriber` it catches
  :class:`~mote.common.events.PostCompactEvent` and clears the frontier, so
  the turn after a compaction re-emits the full map (the earlier one was
  condensed away with the rest of pre-compaction history);
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
from mote.common.events import PostCompactEvent
from mote.common.interface import ObservationSubscriber, TurnContextPriority
from mote.common.text import display_path
from mote.common.utils.prompt_sanitizer import count_tokens
from mote.context.code_map import CodeMap, FileNeighborhood

# When a file defines more than this many symbols, fold the tail behind a
# "(+N more)" summary so one large file cannot dominate the map block.
_MAX_SYMBOLS_PER_FILE = 12
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
    ) -> None:
        self._get_touched_files = get_touched_files
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
        # renders (fully backward compatible).
        self._get_read_state = get_read_state
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
        # signature) that breaks unseen callers. Cleared on PostCompactEvent.
        self._shape_cache: dict[str, dict[str, str]] = {}
        # F3 per-render (recomputed each render, folded into the signature):
        #   _changed_names: path -> [broken qualified names] (removed OR sig-changed)
        #                   — drives the risk label + re-surface fold.
        #   _changed_symbols: path -> [Symbol] still existing (sig-changed only)
        #                   — F2's references targets (a removed symbol can't be queried).
        #   _precise: path -> {qualified_name: [caller display paths]} from F2.
        self._changed_names: dict[str, list[str]] = {}
        self._changed_symbols: dict[str, list] = {}
        self._precise: dict[str, dict[str, list[str]]] = {}

    async def handle(self, event) -> None:
        """Reset the incremental frontier after a compaction (re-emit the full map).

        The prior map rows were persisted into history and condensed away by the
        compaction, so the model no longer has them; clearing ``_reported`` makes
        the next render re-emit every non-trivial file. The in-context frontier is
        also cleared (bodies were condensed away → show ``defines:`` again) and the
        symbol-shape baseline reset (a fresh post-compaction baseline, so the first
        render after does not mis-fire the interface-change risk label). All other
        events ignored.
        """
        if isinstance(event, PostCompactEvent):
            self._reported = {}
            self._in_context = set()
            self._post_compact = True
            self._shape_cache = {}
        return None

    async def render(self, *, cwd: Optional[str] = None) -> Optional[str]:
        touched = self._get_touched_files() if self._get_touched_files else []
        if not touched:
            return None

        # Layer C: whole-repo reverse deps when a repo index is wired; else the
        # touched-set-scoped query (backward compatible).
        repo_importers = getattr(self._repo_index, "importers", None) if self._repo_index else None
        try:
            neighborhoods = self._map.neighborhood(list(touched), repo_importers=repo_importers)
        except Exception:  # noqa: BLE001 — best-effort; never break a turn
            return None

        # Layer B: resolve dangling-import symbols for the changed rows via LSP.
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

    async def _fill_unread_symbols(self, neighborhoods: list) -> None:
        """Resolve dangling-import target symbols via LSP, per neighborhood.

        Best-effort: a raising resolver leaves ``unread_symbols`` empty (the row
        still renders the bare unread path). Only rows that actually have unread
        imports are queried.
        """
        for nb in neighborhoods:
            if not nb.imports_unread:
                continue
            try:
                nb.unread_symbols = await self._map.resolve_unread(nb.path, nb.imports_unread, self._lsp_query)
            except Exception:  # noqa: BLE001 — never break a turn on a bad resolve
                nb.unread_symbols = {}

    # -- F1: read-state / in-context frontier --------------------------------

    def _refresh_in_context(self, neighborhoods: list) -> None:
        """Recompute which touched files' bodies are live in history this turn.

        A file counts as *in context* when the read-state records it was read at
        its *current* on-disk mtime — the body the model saw is still accurate, so
        re-stating its ``defines:``/``calls:`` only wastes attention. It is *not*
        in context when there is no read entry (surfaced purely as a dependency) or
        the recorded mtime is stale (edited since last read — Feature 3 wants to
        re-show it *and* flag the interface risk). With no ``get_read_state`` the
        frontier stays empty → self-description always renders (backward compatible).
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
            "grep/read instead of re-scanning:",
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
        # F1: gate self-description on body-not-in-context. An interface change
        # re-shows defines regardless — the model needs to see what it just broke.
        if nb.symbols and (not in_context or interface_changed):
            out.append(f"    defines: {self._render_symbols(nb.symbols, tier)}")
            if tier < 1:
                calls = self._render_calls(nb.calls)
                if calls:
                    out.append(f"    calls: {calls}")
        if nb.imports:
            targets = ", ".join(display_path(p, cwd) for p in nb.imports)
            out.append(f"    imports: {targets}")
        if nb.imports_unread:
            unread = ", ".join(self._render_unread(p, nb.unread_symbols, cwd, tier) for p in nb.imports_unread)
            out.append(f"    also imports (unread): {unread}")
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

    def _render_unread(self, path: str, unread_symbols: dict, cwd: Optional[str], tier: int) -> str:
        """One unread-import target: ``pkg/other.py (thing, helper)`` when resolved.

        At tier ≥1 the resolved-symbol annotation is dropped to respect the
        token budget — the bare path still orients the model to the dependency.
        """
        display = display_path(path, cwd)
        if tier >= 1:
            return display
        names = unread_symbols.get(path) if unread_symbols else None
        if names:
            return f"{display} ({', '.join(names)})"
        return display

    @staticmethod
    def _render_symbols(symbols: list, tier: int) -> str:
        """Symbol list, folding a long tail and honouring the tier's verbosity.

        Tier 0 shows ``name(sig)`` for the first :data:`_MAX_SYMBOLS_PER_FILE`
        top-level-ish symbols; tiers ≥1 show bare qualified names. Either way a
        folded tail is summarised as ``(+N more)`` so one big file stays bounded.
        """
        head = symbols[:_MAX_SYMBOLS_PER_FILE]
        overflow = len(symbols) - len(head)
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
          (mirrors the Layer B ``resolved`` fold).
        """
        syms = ",".join(s.qualified_name for s in nb.symbols)
        imps = ",".join(nb.imports)  # already sorted by the facade
        unread = ",".join(nb.imports_unread)  # already sorted by the facade
        used = ",".join(nb.imported_by)  # already sorted by the facade
        calls = ",".join(sorted({f"{c.caller}>{c.callee}" for c in nb.calls}))
        # Fold resolved unread symbols so a newly-resolved "defines" view
        # re-surfaces the row (Layer B). Sorted by target for a stable key.
        resolved = ";".join(f"{p}:{','.join(nb.unread_symbols[p])}" for p in sorted(nb.unread_symbols or {}))
        in_ctx = "1" if nb.path in self._in_context else "0"
        risk = ",".join(self._changed_names.get(nb.path, []))
        precise = ";".join(
            f"{sym}:{','.join(callers)}" for sym, callers in sorted((self._precise.get(nb.path) or {}).items())
        )
        return f"{syms}|{imps}|{unread}|{used}|{calls}|{resolved}|{in_ctx}|{risk}|{precise}"


__all__ = ["CodeMapContextSource"]
