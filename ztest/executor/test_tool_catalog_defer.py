#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for tool-search deferral in the ToolCatalog (mote.executor.tool_catalog).

Deferral is the single filter seam both channels route through: a deferred tool's
schema is withheld from ``schemas_for`` (XML) and ``native_specs`` (native) until
it appears in the live revealed set, while ``deferred_index`` always lists the
full deferred menu (byte-stable, independent of the revealed set).
"""
from __future__ import annotations

from mote.common.utils.docstring import first_line
from mote.executor.tool_catalog import SPLIT_TOOLSPEC_DESC, ToolCatalog


class _FakeTool:
    """Minimal BaseTool stand-in: name + fixed builtin schema.

    ``summary()`` mirrors :meth:`BaseTool.summary` — the one-line MENU entry,
    which in the docstring-native design is simply the FIRST line of the tool's
    description (no separate authored field). The search menus (deferred_index /
    split_tool_menu) read it via ``_menu_line``.
    """

    def __init__(self, name: str, desc: str = "", keywords: list[str] | None = None) -> None:
        self.name = name
        self._desc = desc or f"{name} does something."
        self.keywords = keywords or []

    def summary(self) -> str:
        return first_line(self._desc)

    def search_text(self) -> str:
        # Mirrors BaseTool.search_text: summary + recall keywords (search corpus).
        if not self.keywords:
            return self.summary()
        return f"{self.summary()} {' '.join(self.keywords)}"

    def tool_schema(self) -> dict:
        return {"name": self.name, "description": self._desc, "parameters": {}}

    def native_schema(self) -> dict:
        return {"name": self.name, "description": self._desc, "input_schema": {"type": "object"}}


def _catalog(*, deferred=None, revealed=None) -> ToolCatalog:
    revealed_set = set(revealed or [])
    cat = ToolCatalog(deferred=set(deferred or []), get_revealed=lambda: revealed_set)
    cat.register(_FakeTool("Read", "Read a file."), ["Read", "read"])
    cat.register(_FakeTool("ConvertImage", "Convert an image between formats."), ["ConvertImage"])
    return cat


class TestFilter:
    def test_deferred_unrevealed_absent_from_xml_schemas(self):
        # WITHHOLD is the XML-only path (schemas_for): no ``tools=`` prefix cache
        # to protect there, so the schema is dropped until revealed.
        cat = _catalog(deferred=["ConvertImage"])
        builtin = cat.schemas_for("builtin")
        assert "ConvertImage" not in builtin
        assert "Read" in builtin  # non-deferred always present

    def test_deferred_present_on_native_via_split(self):
        # Native tools= is NEVER withheld — an incapable native transport (openai
        # Chat Completions here) uses SPLIT: the corpus tool stays present (name +
        # params) with a stub description, keeping the wire byte-stable. The full
        # description rides the reminder tail (split_tool_menu), not the wire.
        cat = _catalog(deferred=["ConvertImage"])
        specs = cat.native_specs("openai")
        conv = next(s for s in specs if s["function"]["name"] == "ConvertImage")
        assert conv["function"]["description"] == SPLIT_TOOLSPEC_DESC
        assert "parameters" in conv["function"]  # params still on the wire
        assert all("defer_loading" not in s for s in specs)  # no server-side stamp
        # Non-deferred keeps its real description.
        read = next(s for s in specs if s["function"]["name"] == "Read")
        assert read["function"]["description"] == "Read a file."

    def test_split_wire_byte_stable_across_reveal(self):
        # The cache guard for the SPLIT path: revealing must NOT mutate tools=
        # (the stub is constant + revealed-set-independent).
        before = _catalog(deferred=["ConvertImage"]).native_specs("openai")
        after = _catalog(deferred=["ConvertImage"], revealed=["ConvertImage"]).native_specs("openai")
        assert before == after

    def test_revealed_present_in_both_channels(self):
        cat = _catalog(deferred=["ConvertImage"], revealed=["ConvertImage"])
        assert "ConvertImage" in cat.schemas_for("builtin")
        assert "ConvertImage" in {spec["function"]["name"] for spec in cat.native_specs("openai")}

    def test_non_deferred_never_hidden(self):
        cat = _catalog(deferred=["ConvertImage"])
        assert "Read" in cat.schemas_for("builtin")
        assert "Read" in {spec["function"]["name"] for spec in cat.native_specs("openai")}


_CAPABLE_ANTHROPIC = "opus-4"  # supports_native_tool_search → True
_CAPABLE_OPENAI = "gpt-5.4"  # supports_native_tool_search → True
_OLD_ANTHROPIC = "claude-3-5-sonnet"  # supports_native_tool_search → False
_OLD_OPENAI = "gpt-4o"  # supports_native_tool_search → False


class TestAnthropicDeferLoading:
    """Anthropic native path: server-side defer_loading, not client-side hide.

    Corpus tools stay PRESENT on the wire (the API needs every definition to
    expand tool_reference blocks) but carry ``defer_loading:true`` keyed on
    corpus membership — so the ``tools=`` prefix is byte-stable across reveals
    (prompt cache preserved), the opposite of the openai client-side fallback.

    Now capability-gated: the server-side path fires ONLY when the model
    supports native tool search (:func:`supports_native_tool_search`); an old
    Claude falls back to client-side hide — the latent-bug fix.
    """

    def _spec(self, specs, name):
        return next((s for s in specs if s["name"] == name), None)

    def test_corpus_present_with_defer_loading_even_when_unrevealed(self):
        cat = _catalog(deferred=["ConvertImage"])
        specs = cat.native_specs("anthropic", model=_CAPABLE_ANTHROPIC)
        conv = self._spec(specs, "ConvertImage")
        assert conv is not None  # NOT withheld (unlike client-side)
        assert conv.get("defer_loading") is True

    def test_corpus_keeps_defer_loading_when_revealed(self):
        # defer_loading is keyed on corpus membership, NOT the revealed set.
        cat = _catalog(deferred=["ConvertImage"], revealed=["ConvertImage"])
        conv = self._spec(cat.native_specs("anthropic", model=_CAPABLE_ANTHROPIC), "ConvertImage")
        assert conv is not None
        assert conv.get("defer_loading") is True

    def test_non_corpus_has_no_defer_loading(self):
        cat = _catalog(deferred=["ConvertImage"])
        read = self._spec(cat.native_specs("anthropic", model=_CAPABLE_ANTHROPIC), "Read")
        assert read is not None
        assert "defer_loading" not in read

    def test_wire_byte_stable_across_reveal(self):
        # The core cache-preservation guard: revealing a tool must NOT mutate the
        # tools= array (byte-identical), so Anthropic's tools->system->messages
        # cache prefix is never invalidated.
        before = _catalog(deferred=["ConvertImage"]).native_specs("anthropic", model=_CAPABLE_ANTHROPIC)
        after = _catalog(deferred=["ConvertImage"], revealed=["ConvertImage"]).native_specs(
            "anthropic", model=_CAPABLE_ANTHROPIC
        )
        assert before == after

    def test_old_claude_falls_back_to_split(self):
        # LATENT-BUG FIX: an old Claude (no native tool search) must NOT be stamped
        # with defer_loading — the API would reject/drop it. It falls back to the
        # client-side SPLIT: the corpus tool stays present (name + params) with a
        # stub description, so the tools= prefix is still byte-stable (no withhold).
        cat = _catalog(deferred=["ConvertImage"])
        conv = self._spec(cat.native_specs("anthropic", model=_OLD_ANTHROPIC), "ConvertImage")
        assert conv is not None  # present (split), not withheld
        assert conv["description"] == SPLIT_TOOLSPEC_DESC
        assert "defer_loading" not in conv

    def test_no_model_falls_back_to_split(self):
        # No model → capability unknown → SPLIT (defensive: never withhold on the
        # native wire, never stamp defer_loading).
        cat = _catalog(deferred=["ConvertImage"])
        conv = self._spec(cat.native_specs("anthropic"), "ConvertImage")
        assert conv is not None
        assert conv["description"] == SPLIT_TOOLSPEC_DESC
        assert "defer_loading" not in conv

    def test_openai_chat_completions_uses_split(self):
        # The Chat Completions "openai" envelope has no server-side path, so even a
        # capable model runs SPLIT there (present with stub, byte-stable), NOT the
        # server-side stamp — defer_loading is Responses/Anthropic only.
        cat = _catalog(deferred=["ConvertImage"])
        specs = cat.native_specs("openai", model=_CAPABLE_OPENAI)
        conv = next(s for s in specs if s["function"]["name"] == "ConvertImage")
        assert conv["function"]["description"] == SPLIT_TOOLSPEC_DESC
        assert all("defer_loading" not in s for s in specs)

    def test_no_defer_loading_when_nothing_deferred(self):
        cat = _catalog()
        specs = cat.native_specs("anthropic", model=_CAPABLE_ANTHROPIC)
        assert all("defer_loading" not in s for s in specs)


class TestOpenAIResponsesDeferLoading:
    """OpenAI Responses native path: symmetric with Anthropic, flat envelope.

    A capable model (gpt-5.4+) on the ``openai_responses`` envelope gets the
    server-side defer_loading treatment — corpus tools present-with-flag,
    byte-stable across reveal — using the FLAT Responses function shape. An old
    OpenAI model falls back to client-side hide.
    """

    def _spec(self, specs, name):
        return next((s for s in specs if s["name"] == name), None)

    def test_corpus_present_with_defer_loading(self):
        cat = _catalog(deferred=["ConvertImage"])
        specs = cat.native_specs("openai_responses", model=_CAPABLE_OPENAI)
        conv = self._spec(specs, "ConvertImage")
        assert conv is not None
        assert conv.get("defer_loading") is True
        # FLAT Responses function shape: type/name/description/parameters at top level.
        assert conv["type"] == "function"
        assert "parameters" in conv
        assert "function" not in conv  # NOT the nested Chat Completions shape

    def test_wire_byte_stable_across_reveal(self):
        before = _catalog(deferred=["ConvertImage"]).native_specs("openai_responses", model=_CAPABLE_OPENAI)
        after = _catalog(deferred=["ConvertImage"], revealed=["ConvertImage"]).native_specs(
            "openai_responses", model=_CAPABLE_OPENAI
        )
        assert before == after

    def test_non_corpus_has_no_defer_loading(self):
        cat = _catalog(deferred=["ConvertImage"])
        read = self._spec(cat.native_specs("openai_responses", model=_CAPABLE_OPENAI), "Read")
        assert read is not None
        assert "defer_loading" not in read

    def test_old_openai_falls_back_to_split(self):
        # An old GPT (no native tool search) → client-side SPLIT (present with the
        # stub description on the flat Responses shape), no defer_loading stamp.
        cat = _catalog(deferred=["ConvertImage"])
        conv = self._spec(cat.native_specs("openai_responses", model=_OLD_OPENAI), "ConvertImage")
        assert conv is not None
        assert conv["description"] == SPLIT_TOOLSPEC_DESC
        assert "defer_loading" not in conv


class TestDeferredIndex:
    def test_validation_view_lists_deferred_regardless_of_revealed(self):
        # include_revealed=True (default) = the identity/validation view: a
        # revealed tool must still be recognised as a deferred name (reveal_tools
        # checks a name against this), so the whole corpus is listed either way.
        cat = _catalog(deferred=["ConvertImage"])
        assert cat.deferred_index() == {"ConvertImage": "Convert an image between formats."}

        cat2 = _catalog(deferred=["ConvertImage"], revealed=["ConvertImage"])
        assert cat2.deferred_index() == {"ConvertImage": "Convert an image between formats."}

    def test_display_view_drops_revealed(self):
        # include_revealed=False = the DISPLAY view for the reminder tail: a
        # revealed tool's schema is already live, so it drops OUT of the "search
        # to enable" menu (the menu only ever shrinks). No cache churn — the tail
        # rides after the cache breakpoint.
        cat = _catalog(deferred=["ConvertImage"])
        assert cat.deferred_index(include_revealed=False) == {"ConvertImage": "Convert an image between formats."}

        cat2 = _catalog(deferred=["ConvertImage"], revealed=["ConvertImage"])
        assert cat2.deferred_index(include_revealed=False) == {}

    def test_empty_when_nothing_deferred(self):
        cat = _catalog()
        assert cat.deferred_index() == {}
        assert cat.deferred_index(include_revealed=False) == {}

    def test_index_excludes_non_deferred(self):
        cat = _catalog(deferred=["ConvertImage"])
        assert "Read" not in cat.deferred_index()


class TestDeferredSearchIndex:
    """deferred_search_index: the MATCH corpus (summary + recall keywords).

    The search-only sibling of deferred_index. The DISPLAY menu stays a pure
    one-line summary (byte-stable, small); this corpus folds in each tool's
    recall keywords so SearchTools matches synonyms the summary omits — WITHOUT
    those words ever reaching a menu or the wire.
    """

    def _catalog_with_kw(self, *, deferred, revealed=None):
        revealed_set = set(revealed or [])
        cat = ToolCatalog(deferred=set(deferred), get_revealed=lambda: revealed_set)
        cat.register(_FakeTool("Read", "Read a file."), ["Read"])
        cat.register(
            _FakeTool("Web", "Search the web.", keywords=["google", "internet", "搜索"]),
            ["Web"],
        )
        return cat

    def test_search_index_folds_in_keywords(self):
        cat = self._catalog_with_kw(deferred=["Web"])
        assert cat.deferred_search_index() == {"Web": "Search the web. google internet 搜索"}

    def test_display_index_stays_summary_only(self):
        # The DISPLAY menu must NOT carry keywords — only the search corpus does.
        cat = self._catalog_with_kw(deferred=["Web"])
        assert cat.deferred_index() == {"Web": "Search the web."}

    def test_no_keywords_equals_summary(self):
        # A tool with no keywords: search text == display text (no divergence).
        cat = self._catalog_with_kw(deferred=["Web"])
        cat.register(_FakeTool("Bare", "A bare tool."), ["Bare"])
        cat_bare = ToolCatalog(deferred={"Bare"}, get_revealed=lambda: set())
        cat_bare.register(_FakeTool("Bare", "A bare tool."), ["Bare"])
        assert cat_bare.deferred_search_index() == {"Bare": "A bare tool."}

    def test_search_index_byte_stable_across_reveal(self):
        unrevealed = self._catalog_with_kw(deferred=["Web"])
        revealed = self._catalog_with_kw(deferred=["Web"], revealed=["Web"])
        assert unrevealed.deferred_search_index() == revealed.deferred_search_index()

    def test_empty_when_nothing_deferred(self):
        cat = self._catalog_with_kw(deferred=[])
        assert cat.deferred_search_index() == {}


class TestSplitToolMenu:
    """split_tool_menu: brief hints for UNREVEALED split-path deferred tools.

    The ephemeral half of split — a one-line hint per not-yet-revealed corpus
    tool on the reminder tail (never the wire). A revealed tool drops OUT of the
    menu: its full description is persisted into the conversation on discovery
    (see SearchTools), so the menu only ever shrinks.
    """

    def test_unrevealed_shows_brief_description(self):
        cat = _catalog(deferred=["ConvertImage"])
        menu = cat.split_tool_menu()
        assert menu == {"ConvertImage": "Convert an image between formats."}

    def test_revealed_drops_out_of_menu(self):
        # Once revealed, the tool leaves the ephemeral menu — its full
        # description now lives persisted in history, not re-sent here.
        revealed = _catalog(deferred=["ConvertImage"], revealed=["ConvertImage"])
        assert "ConvertImage" not in revealed.split_tool_menu()

    def test_brief_is_the_summary_first_line(self):
        # The menu blurb is the tool's summary() = the FIRST line of its
        # docstring-native description (the authored one-line menu sentence),
        # NOT a mechanical join of the whole body.
        multiline = "First line.\nSecond line with detail.\nThird line."
        unrevealed = ToolCatalog(deferred={"Big"}, get_revealed=lambda: set())
        unrevealed.register(_FakeTool("Big", multiline), ["Big"])
        assert unrevealed.split_tool_menu()["Big"] == "First line."

    def test_excludes_non_deferred(self):
        cat = _catalog(deferred=["ConvertImage"])
        assert "Read" not in cat.split_tool_menu()

    def test_empty_when_nothing_deferred(self):
        assert _catalog().split_tool_menu() == {}


class TestDescribeDeferred:
    """describe_deferred: full (multi-line) prose the SPLIT path strips off the wire.

    SearchTools reads this on reveal to persist each revealed tool's real
    description into the conversation + ResourceRegistry.
    """

    def test_returns_full_multiline_description(self):
        multiline = "First line.\nSecond line with detail."
        cat = ToolCatalog(deferred={"Big"}, get_revealed=lambda: set())
        cat.register(_FakeTool("Big", multiline), ["Big"])
        assert cat.describe_deferred(["Big"]) == {"Big": multiline}


class TestMenuLine:
    """The search-menu blurb is the tool's docstring-native summary.

    Both menu builders (deferred_index, split_tool_menu) share ``_menu_line``,
    which reads :meth:`BaseTool.summary` — the FIRST line of the tool's
    description. There is no separate authored menu field any more: author once
    in the docstring, and the summary line doubles as the menu entry.
    """

    def test_index_uses_summary_first_line(self):
        full = "Short catalogue blurb.\nWith several detail lines that would bloat a menu."
        cat = ToolCatalog(deferred={"Tool"}, get_revealed=lambda: set())
        cat.register(_FakeTool("Tool", full), ["Tool"])
        # Only the first line rides the menu — the body stays off it.
        assert cat.deferred_index() == {"Tool": "Short catalogue blurb."}

    def test_split_menu_uses_summary_first_line(self):
        full = "Short catalogue blurb.\nWith several detail lines."
        cat = ToolCatalog(deferred={"Tool"}, get_revealed=lambda: set())
        cat.register(_FakeTool("Tool", full), ["Tool"])
        assert cat.split_tool_menu() == {"Tool": "Short catalogue blurb."}

    def test_falls_back_to_name_without_summary(self):
        # A bare stand-in that exposes no summary() → the name is the blurb.
        class _Bare:
            name = "Bare"

            def tool_schema(self) -> dict:
                return {"name": self.name, "description": "", "parameters": {}}

            def native_schema(self) -> dict:
                return {"name": self.name, "description": "", "input_schema": {"type": "object"}}

        cat = ToolCatalog(deferred={"Bare"}, get_revealed=lambda: set())
        cat.register(_Bare(), ["Bare"])
        assert cat.deferred_index() == {"Bare": "Bare"}


class TestDescribeDeferredNames:
    """describe_deferred resolves only names in the deferred corpus."""

    def test_only_deferred_names_resolve(self):
        # A non-deferred name (Read) is not in the corpus → skipped.
        cat = _catalog(deferred=["ConvertImage"])
        out = cat.describe_deferred(["ConvertImage", "Read", "Bogus"])
        assert set(out) == {"ConvertImage"}
        assert out["ConvertImage"] == "Convert an image between formats."

    def test_empty_for_no_names(self):
        assert _catalog(deferred=["ConvertImage"]).describe_deferred([]) == {}
