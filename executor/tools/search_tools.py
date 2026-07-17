"""``SearchTools`` — discover and reveal deferred (hidden) tools on demand.

Peripheral tools can be *deferred* (declared in ``RoleSchema.deferred_tools``):
their full schema is withheld from the model every turn to keep the steady
per-turn token cost flat. Only a compact index (name + one-line description) of
the deferred set rides the per-turn reminder. When the model needs one of those
tools it calls this meta-tool with keywords; the matching tools are *revealed*
(recorded on ``RoleState``) so their full schema is sent on the next turn and
they become directly callable.

Discovery is local (not a provider-native server-side search), so it works
identically on both the XML and native tool-use channels. Two ways to select
what to reveal, usable together: a scalar ``query`` string (heuristic keyword
match over the deferred tools' names + one-line descriptions — works on both
channels) and an explicit ``names`` list (reveal those exact tools directly,
skipping the heuristic — native-only, since the XML protocol delivers every arg
as a string, though a comma/space-joined string is accepted there too). The two
results are UNIONed. Revelation is durable: the revealed set lives on
``RoleState`` (survives session resume), the single authority for *which* tools
are revealed.

The result body itself is deliberately minimal: a one-line "loaded, now
callable" confirmation, NOT an echo of each revealed tool's schema/description.
Re-stating the schema in the body would be redundant tokens — the full schema
reaches the model on the NEXT turn by the channel's own mechanism (server-side
``tool_reference`` expansion / the XML catalogue re-emitting the now-revealed
schema / the split wire's name+params). The model only needs to know the load
succeeded.

On the client-side SPLIT native path (an incapable native model), a corpus
tool's NAME + ``input_schema`` ride the ``tools=`` wire with only a stub
description; the real prose is stripped off the byte-stable prefix. When this
tool reveals such a tool it *persists* that full description as a
``kind="tool"`` sticky resource in the ResourceRegistry (a side-effect, NOT on
the result body) so a post-compaction summary re-projects it, exactly like an
inline Skill body. This mirrors the Skill tool's inline-body pattern: the
description is a loaded capability body, paid once into the cached prefix, not
re-sent uncached on the reminder tail every turn.

The result is **not** ``reconstructable`` because the body carries
``data={"tool_references": ...}`` — the private wire projection the Anthropic
native path expands into full tool definitions. Folding the body away would
lose those reference blocks. The revealed *set* itself lives durably on
RoleState regardless.
"""

from __future__ import annotations

import re
from typing import Optional

from mote.common.exception import ToolValidationError
from mote.executor.base_tool import BaseTool
from mote.executor.capability_types import DescribeDeferredTools, ListDeferredTools, RegisterResource, RevealTools
from mote.executor.tool_registry import register_tool
from mote.executor.tool_result import ToolResult

_MSG_NO_DEFERRED = "No additional tools are available to search."
_MSG_NO_MATCH = "No additional tools match '{query}'. Available to search: {names}."
_MSG_NO_INPUT = "Provide 'query' (keywords) and/or 'names' (exact tool names) to search."
_MSG_UNKNOWN_NAMES = "Unknown tool name(s): {unknown}. Available to search: {names}."


@register_tool
class SearchTools(BaseTool):
    """Search for and reveal additional (deferred) tools by keyword."""

    name = "SearchTools"
    aliases: list[str] = ["search_tools"]
    requires = ("list_deferred_tools", "reveal_tools", "describe_deferred_tools", "register_resource")
    # NOT reconstructable: the result body carries ``data={"tool_references": …}``
    # — the private wire projection the Anthropic native path expands into full
    # tool definitions. Folding the body away would lose those reference blocks.
    # (The revealed *set* still lives durably on RoleState regardless; the SPLIT
    # path's descriptions survive via the sticky resource, not this body.)
    reconstructable = False

    # Injected from Role by bind():
    list_deferred_tools: ListDeferredTools
    reveal_tools: RevealTools
    describe_deferred_tools: DescribeDeferredTools
    # Defaults to a no-op stub so the tool works when bound without a Role
    # (standalone / tests) — description persistence is best-effort bookkeeping.
    register_resource: RegisterResource = staticmethod(lambda **k: None)

    async def call(self, *, query: str = "", names: Optional[list[str]] = None) -> ToolResult:
        """Discover and enable deferred tools — by keyword and/or by exact name.

        Discover and enable additional tools that are not loaded by default. Some
        peripheral tools are hidden to keep the toolset focused; the reminder
        lists their names + a one-line description under "Additional tools". Two
        ways to reveal them, usable together (their results are UNIONed):

          - ``query``: space/comma-separated keywords describing the capability
            you need (heuristic match by capability, not exact name — e.g.
            "convert image", "database query"). Every hidden tool sharing any
            keyword is revealed.
          - ``names``: an explicit list of the exact tool names to reveal (skips
            the heuristic). Use this when you already know the tool's name from
            the "Additional tools" menu.

        Revealed tools become available (their full schema arrives, and they are
        directly callable) on the next turn.

        Args:
            query: Capability keywords to match (fuzzy). Optional if ``names`` given.
            names: Exact tool names to reveal (skips matching). Optional if ``query``
                given.

        Returns:
            The tools revealed (name + description), now callable next turn.
        """
        index = self.list_deferred_tools()
        if not index:
            return ToolResult(output=_MSG_NO_DEFERRED)

        requested = self._normalize_names(names)
        if not query.strip() and not requested:
            raise ToolValidationError(_MSG_NO_INPUT)

        # Explicit names: exact (case-insensitive) match against the deferred set;
        # any unknown name is a hard error so the model learns it mis-typed rather
        # than silently revealing nothing.
        by_name: set[str] = set()
        if requested:
            canonical = {n.lower(): n for n in index}
            unknown = [n for n in requested if n.lower() not in canonical]
            if unknown:
                raise ToolValidationError(
                    _MSG_UNKNOWN_NAMES.format(unknown=", ".join(unknown), names=", ".join(sorted(index)))
                )
            by_name = {canonical[n.lower()] for n in requested}

        # Heuristic keyword match (only when a query is given).
        by_query: set[str] = set()
        if query.strip():
            latin, cjk_runs = self._parse_query(query)
            by_query = {name for name, desc in index.items() if self._matches(latin, cjk_runs, name, desc)}

        matched = sorted(by_name | by_query)
        if not matched:
            return ToolResult(output=_MSG_NO_MATCH.format(query=query, names=", ".join(sorted(index))))

        revealed = self.reveal_tools(matched)
        # Persist the revealed tools' full descriptions for the client-side SPLIT
        # native path (where the prose is stripped off the byte-stable ``tools=``
        # wire): registering them as sticky resources puts the prose back after a
        # compaction discards the head. On the server-side / XML paths the schema
        # is (or becomes, next turn) live on the channel, so this is a harmless
        # bookkeep. NOTE: this is a side-effect only — the description does NOT
        # ride the result body (see below).
        self._persist_descriptions(self.describe_deferred_tools(revealed))
        # The result only CONFIRMS the load; it does NOT echo each tool's schema
        # or description. Re-stating the schema here is redundant tokens — the
        # full schema reaches the model on the NEXT turn by the channel's own
        # mechanism (server-side ``tool_reference`` expansion / the XML catalog
        # re-emitting the now-revealed schema / the split wire's name+params).
        # So a plain "loaded, now callable" line is all the model needs.
        revealed_names = ", ".join(revealed)
        output = f"Loaded {len(revealed)} tool(s): {revealed_names}. They are now available to call."
        # ``tool_references`` rides ToolResult.data → ToolMessage.tool_references →
        # the ``_tool_references`` private wire key, so on the Anthropic native
        # (server-side tool-search) path the tool_result is rendered as
        # ``tool_reference`` blocks that the API expands into full definitions.
        # Other providers/XML ignore ``data`` — the human-readable ``output`` and
        # the durable RoleState reveal drive the client-side fallback there.
        return ToolResult(output=output, data={"tool_references": revealed})

    def _persist_descriptions(self, descriptions: dict[str, str]) -> None:
        """Register each revealed tool's full description as a sticky resource.

        Mirrors :meth:`Skill._register_resource`: a loaded capability body is
        registered under ``kind="tool"`` so the ResourceRegistry re-projects it
        after an autocompaction discards the head. Best-effort and non-throwing
        (the ``register_resource`` capability defaults to a no-op stub when the
        tool is bound without a Role), so bookkeeping never breaks the result.
        """
        for name, desc in descriptions.items():
            if not desc:
                continue
            try:
                self.register_resource(id=name, kind="tool", content=desc)
            except Exception:  # never let bookkeeping break the tool result
                pass

    # Latin word tokenizer: splits on any run of non-alphanumerics AND on
    # camelCase / PascalCase humps so a tool name like ``ConvertImage`` yields
    # {"convert", "image"}. Latin matching is whole-token set overlap (NOT
    # substring), so query "image" hits ``ConvertImage`` but query "me" no longer
    # spuriously matches ``get_me``/``ImageResize`` — it must be a standalone word.
    _LATIN_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|[a-z]+|[0-9]+")
    # CJK (Chinese ideographs / Japanese kana / Korean hangul) has no whitespace
    # word boundaries, so the Latin whole-token model can't apply. Following
    # Lucene's CJKBigramFilter, a multi-char CJK run is matched by overlapping
    # character BIGRAM set overlap (order-independent partial match: query
    # "查询数据" still hits "数据库查询" via the shared {数据,查询} bigrams, which a
    # whole-run substring probe would miss). A lone CJK char (no bigram) falls
    # back to a substring probe so single-char queries still resolve.
    _CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff\uac00-\ud7a3]+")

    # Latin stopwords stripped from the QUERY only (never the corpus). These are
    # high-frequency function words + generic filler verbs/nouns that a model
    # naturally pads a request with ("run a shell command", "delegate a task")
    # but that carry NO discriminative signal — they overlap almost every tool's
    # summary and revealed a flood of unrelated tools (precision noise). Removing
    # them from the query is the standard IR precision fix: recall is UNAFFECTED
    # (every real content keyword survives) and there is an EMPTY-FALLBACK — a
    # query made entirely of stopwords keeps its raw tokens so we never turn a hit
    # into a miss. Query-side only: a corpus keyword like "task"/"run" is a
    # deliberate recall term and must stay matchable, so the corpus is never
    # filtered. Latin only — CJK bigram matching has no such function-word class.
    _STOPWORDS: frozenset[str] = frozenset(
        {
            "a",
            "an",
            "the",
            "this",
            "that",
            "these",
            "those",
            "of",
            "to",
            "for",
            "in",
            "on",
            "at",
            "by",
            "with",
            "from",
            "into",
            "over",
            "as",
            "and",
            "or",
            "is",
            "are",
            "be",
            "it",
            "its",
            "i",
            "me",
            "you",
            "some",
            "any",
            "my",
            "your",
            "few",
            "please",
            "want",
            "need",
            "task",
            "work",
            "thing",
            "something",
            "items",
            "item",
            "stuff",
            "run",
            "do",
            "does",
            "get",
            "make",
            "made",
            "use",
            "using",
            "let",
        }
    )

    @staticmethod
    def _normalize_names(names: Optional[list[str]]) -> list[str]:
        """Coerce the ``names`` arg into a clean list of tool names.

        Native channel delivers a real ``list[str]``; XML delivers a single string
        (its protocol has no lists), so a comma/space-separated string is split.
        Blanks are dropped. ``None``/empty → ``[]``.
        """
        if not names:
            return []
        if isinstance(names, str):
            names = re.split(r"[,\s]+", names)
        return [n.strip() for n in names if n and n.strip()]

    @classmethod
    def _tokenize(cls, text: str) -> set[str]:
        """Lowercased Latin word tokens: split on non-alphanumerics + camelCase humps."""
        return {tok.lower() for tok in cls._LATIN_RE.findall(text) if tok}

    @classmethod
    def _strip_stopwords(cls, tokens: set[str]) -> set[str]:
        """Drop Latin stopwords from a QUERY token set, with an empty-fallback.

        Removes non-discriminative function/filler words so they stop matching
        every tool. If that would empty the set (a query made only of stopwords),
        the raw tokens are kept — recall is never sacrificed for precision.
        """
        kept = tokens - cls._STOPWORDS
        return kept if kept else tokens

    @classmethod
    def _cjk_runs(cls, text: str) -> list[str]:
        """Lowercased runs of contiguous CJK characters."""
        return [run.lower() for run in cls._CJK_RE.findall(text)]

    @staticmethod
    def _bigrams(run: str) -> set[str]:
        """Overlapping character bigrams of a run (empty for a single char)."""
        return {run[i : i + 2] for i in range(len(run) - 1)}

    @classmethod
    def _parse_query(cls, query: str) -> tuple[set[str], list[str]]:
        """Split a query into (Latin word tokens, contiguous CJK runs).

        The Latin tokens are stopword-filtered (query-side only, empty-fallback)
        so non-discriminative filler ("a", "run", "task") stops matching every
        tool; CJK runs pass through unfiltered (no function-word class there).
        """
        return cls._strip_stopwords(cls._tokenize(query)), cls._cjk_runs(query)

    @classmethod
    def _matches(cls, latin: set[str], cjk_runs: list[str], name: str, desc: str) -> bool:
        """True when the query matches the tool's name or description.

        Latin keywords match as whole tokens (word-boundary overlap, no substring
        false positives). CJK runs match by character-bigram overlap (multi-char)
        or substring probe (single char) against the raw name+description.
        """
        haystack = f"{name} {desc}"
        if latin & cls._tokenize(haystack):
            return True
        if not cjk_runs:
            return False
        hay_lower = haystack.lower()
        hay_bigrams = {bg for run in cls._cjk_runs(haystack) for bg in cls._bigrams(run)}
        for run in cjk_runs:
            bigrams = cls._bigrams(run)
            if bigrams:
                if bigrams & hay_bigrams:
                    return True
            elif run in hay_lower:  # lone CJK char → substring probe
                return True
        return False
