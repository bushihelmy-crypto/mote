#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the ``SearchTools`` meta-tool (mote.product.toolsets.builtin.search_tools).

``SearchTools`` is the discovery entry point for *deferred* (hidden) tools: the
model calls it with keywords, the matching deferred tools are revealed (recorded
on RoleState via the ``reveal_tools`` capability), and the result names them.
These tests drive it through the same ``CapRole`` capability allowlist the real
Role publishes, so binding + dispatch mirror production.
"""
from __future__ import annotations

import pytest

from mote.product.toolsets.builtin.search_tools import SearchTools
from mote.runtime.errors import ToolValidationError

from .conftest import CapRole, bind, run


def _role(index: dict[str, str]) -> CapRole:
    role = CapRole()
    role.deferred_index = dict(index)
    return role


def _call(role: CapRole, query: str):
    tool = bind(SearchTools(), role)
    return run(tool.call(query=query))


def _call_kw(role: CapRole, **kwargs):
    """Call SearchTools with arbitrary kwargs (query and/or names)."""
    tool = bind(SearchTools(), role)
    return run(tool.call(**kwargs))


INDEX = {
    "ConvertImage": "Convert an image between formats (png, jpeg, webp).",
    "QueryDatabase": "Run a read-only SQL query against the project database.",
    "SendEmail": "Send an email to a recipient.",
}

# Deferred tools described in Chinese — the model may search in the same language.
CJK_INDEX = {
    "ConvertImage": "转换图片格式，支持 png、jpeg、webp。",
    "QueryDatabase": "对项目数据库执行只读 SQL 查询。",
    "SendEmail": "发送邮件给指定收件人。",
}


class TestSearch:
    def test_keyword_match_reveals_tool(self):
        role = _role(INDEX)
        result = _call(role, "image convert")
        assert result.success
        # ConvertImage matches on both "image" and "convert".
        assert "ConvertImage" in result.output
        assert role.revealed == {"ConvertImage"}
        # data carries the discovered names under ``tool_references`` (the seam
        # feeding ToolMessage.tool_references → the Anthropic tool_reference wire).
        assert result.data == {"tool_references": ["ConvertImage"]}

    def test_reveal_result_states_the_stateful_handoff_precondition(self):
        role = _role(INDEX)
        result = _call(role, "image convert")

        assert "action=handoff requires an existing runtime" in result.output
        assert "normal non-handoff call first" in result.output

    def test_description_keyword_match(self):
        role = _role(INDEX)
        # "sql" only appears in QueryDatabase's description, not its name.
        result = _call(role, "sql")
        assert role.revealed == {"QueryDatabase"}
        assert "QueryDatabase" in result.output

    def test_multiple_matches_revealed(self):
        role = _role(INDEX)
        # "email" -> SendEmail (name), "database" -> QueryDatabase (name + desc).
        # Two distinct keywords reveal two tools — proves multi-reveal + sorted.
        result = _call(role, "email database")
        assert role.revealed == {"QueryDatabase", "SendEmail"}

    def test_comma_separated_keywords(self):
        role = _role(INDEX)
        result = _call(role, "image, email")
        assert role.revealed == {"ConvertImage", "SendEmail"}

    def test_no_match_reveals_nothing(self):
        role = _role(INDEX)
        result = _call(role, "kubernetes")
        assert result.success  # a miss is not a failure
        assert role.revealed == set()
        assert "No additional tools match" in result.output

    def test_empty_deferred_set(self):
        role = _role({})
        result = _call(role, "anything")
        assert result.success
        assert role.revealed == set()
        assert "No additional tools are available" in result.output

    def test_reveal_is_idempotent(self):
        role = _role(INDEX)
        _call(role, "image")
        _call(role, "image")
        assert role.revealed == {"ConvertImage"}

    def test_camelcase_name_tokenized(self):
        role = _role(INDEX)
        # "convert" is a camelCase hump of ConvertImage's name (not a whole word
        # on its own) — the tokenizer splits ConvertImage -> {convert, image}.
        result = _call(role, "convert")
        assert role.revealed == {"ConvertImage"}

    def test_substring_no_longer_spuriously_matches(self):
        role = _role(INDEX)
        # "mage" is a SUBSTRING of "Image"/"image" but not a whole token, so the
        # word-boundary matcher must NOT reveal ConvertImage (the old substring
        # matcher would have). This is the false-positive the change eliminates.
        result = _call(role, "mage")
        assert role.revealed == set()
        assert "No additional tools match" in result.output


class TestChineseQuery:
    """CJK has no word boundaries → CJK query runs match as substring probes
    against the raw name+description (the whole-token model applies to Latin
    only). Without this the Latin tokenizer would drop CJK chars → empty query."""

    def test_chinese_keyword_matches_chinese_description(self):
        role = _role(CJK_INDEX)
        result = _call(role, "图片")
        assert role.revealed == {"ConvertImage"}
        assert "ConvertImage" in result.output

    def test_chinese_bigram_matches(self):
        role = _role(CJK_INDEX)
        # "数据" shares the bigram {数据} with "数据库" in QueryDatabase's description.
        result = _call(role, "数据")
        assert role.revealed == {"QueryDatabase"}

    def test_chinese_bigram_order_independent(self):
        role = _role(CJK_INDEX)
        # "查询数据" reorders the description's words but shares bigrams {查询,数据}
        # with "只读 SQL 查询" + "数据库" — character-bigram overlap catches this
        # where a whole-run substring probe ("查询数据" not literally present) would
        # miss. This is the concrete win of the Lucene-style CJK bigram model.
        result = _call(role, "查询数据")
        assert role.revealed == {"QueryDatabase"}

    def test_lone_cjk_char_substring_fallback(self):
        role = _role(CJK_INDEX)
        # A single CJK char produces no bigram → falls back to a substring probe.
        result = _call(role, "图")
        assert role.revealed == {"ConvertImage"}

    def test_chinese_no_spurious_match(self):
        role = _role(CJK_INDEX)
        result = _call(role, "图片")
        assert "SendEmail" not in result.output

    def test_mixed_chinese_and_latin_query(self):
        role = _role(CJK_INDEX)
        # Both halves resolve against their own alphabet: "邮件" hits SendEmail via
        # a CJK bigram on its Chinese description; "convert" hits ConvertImage via
        # a Latin camelCase token on its English name.
        result = _call(role, "邮件 convert")
        assert role.revealed == {"ConvertImage", "SendEmail"}

    def test_latin_query_against_latin_index_unaffected(self):
        # The Latin path is untouched when no CJK is present.
        role = _role(INDEX)
        result = _call(role, "图片")  # pure-CJK query, Latin-only index
        assert role.revealed == set()  # no CJK in the index to substring-match


class TestPersistDescriptions:
    """On reveal the FULL description is persisted (result body + sticky resource).

    The SPLIT native path strips a corpus tool's prose off the byte-stable
    ``tools=`` wire; SearchTools puts it back into the cached conversation
    instead of re-sending it uncached on the reminder tail every turn.
    """

    # Richer multi-line descriptions than the one-line index rows.
    FULL = {
        "ConvertImage": "Convert an image between formats.\nSupports png, jpeg, webp.\nLossless where possible.",
        "QueryDatabase": "Run a read-only SQL query.\nReturns rows as JSON.",
    }

    def _role_with_full(self) -> CapRole:
        role = _role(INDEX)
        role.deferred_descriptions = dict(self.FULL)
        return role

    def test_full_description_persisted_not_echoed(self):
        role = self._role_with_full()
        result = _call(role, "image convert")
        # The full description is PERSISTED as a sticky resource (for the SPLIT
        # path's post-compaction re-projection) but is NOT echoed in the result
        # body — the body only confirms the load.
        kind, content = role.registered_resources["ConvertImage"]
        assert kind == "tool"
        assert "Supports png, jpeg, webp." in content
        assert "Lossless where possible." in content
        assert "Supports png, jpeg, webp." not in result.output
        assert "Lossless where possible." not in result.output
        # The confirmation names the revealed tool and says it is now callable.
        assert "ConvertImage" in result.output

    def test_registers_sticky_resource_kind_tool(self):
        role = self._role_with_full()
        _call(role, "image convert")
        assert "ConvertImage" in role.registered_resources
        kind, content = role.registered_resources["ConvertImage"]
        assert kind == "tool"
        assert content == self.FULL["ConvertImage"]

    def test_only_revealed_tools_persisted(self):
        role = self._role_with_full()
        _call(role, "image")  # reveals ConvertImage only
        assert set(role.registered_resources) == {"ConvertImage"}

    def test_falls_back_to_index_when_no_full_description(self):
        # No ``deferred_descriptions`` set → describe returns the one-line index
        # text, which is still persisted (better than nothing) and shown.
        role = _role(INDEX)
        result = _call(role, "email")
        assert "SendEmail" in role.registered_resources
        kind, content = role.registered_resources["SendEmail"]
        assert kind == "tool"
        assert content == INDEX["SendEmail"]
        assert "SendEmail" in result.output


class TestStopwords:
    """Query-side stopword filtering: precision fix that never costs recall.

    High-frequency filler ("a", "run", "task") is stripped from the QUERY (never
    the corpus) so it stops matching every tool. Recall is unaffected — every
    content keyword survives — and a query made entirely of stopwords keeps its
    raw tokens (empty-fallback) so a hit never degrades to a miss.
    """

    # A corpus where a generic filler word ("run") appears in one tool's text,
    # so an unfiltered query "run a report" would spuriously match it.
    NOISE_INDEX = {
        "QueryDatabase": "Run a read-only SQL query against the project database.",
        "SendEmail": "Send an email to a recipient.",
    }

    def test_stopwords_stripped_from_query(self):
        # "run"/"a" are stopwords → the query reduces to {report}, which matches
        # NOTHING here, instead of spuriously revealing QueryDatabase via "run".
        role = _role(self.NOISE_INDEX)
        result = _call(role, "run a report")
        assert role.revealed == set()
        assert "No additional tools match" in result.output

    def test_content_keyword_survives_alongside_stopwords(self):
        # Filler is dropped but the real keyword ("email") still resolves.
        role = _role(self.NOISE_INDEX)
        _call(role, "send an email to someone")
        assert role.revealed == {"SendEmail"}

    def test_content_word_survives_stopword_padding(self):
        # "run"/"the" are stopwords but "query" is not → it survives filtering
        # and resolves QueryDatabase. Proves filtering removes only filler, never
        # the discriminative term buried in it.
        role = _role(self.NOISE_INDEX)
        result = _call(role, "run the query")
        assert role.revealed == {"QueryDatabase"}

    def test_pure_stopword_query_falls_back_to_raw(self):
        # "a the of" are ALL stopwords → filtering would empty the set, so the
        # empty-fallback keeps the raw tokens and matching proceeds exactly as if
        # no filtering happened. The guarantee is "never a miss you'd otherwise
        # hit": the pure-stopword query yields the SAME result as the unfiltered
        # matcher would. (Degenerate query — but recall is never sacrificed.)
        role = _role(INDEX)
        result = _call(role, "a the of")
        assert result.success
        # Same as feeding the raw (unfiltered) tokens through the matcher.
        raw = SearchTools._tokenize("a the of")
        _, cjk = SearchTools._parse_query("a the of")
        expected = {n for n, d in INDEX.items() if SearchTools._matches(raw, cjk, n, d)}
        assert role.revealed == expected

    def test_strip_stopwords_empty_fallback_unit(self):
        # Unit: an all-stopword token set returns itself (never empty).
        assert SearchTools._strip_stopwords({"a", "the"}) == {"a", "the"}
        # A mixed set drops only the stopwords.
        assert SearchTools._strip_stopwords({"a", "image", "the"}) == {"image"}


class TestExplicitNames:
    """The ``names`` param reveals exact tools directly, bypassing the heuristic."""

    def test_names_reveal_exact_tool(self):
        role = _role(INDEX)
        result = _call_kw(role, names=["ConvertImage"])
        assert result.success
        assert role.revealed == {"ConvertImage"}
        assert result.data == {"tool_references": ["ConvertImage"]}

    def test_names_case_insensitive(self):
        role = _role(INDEX)
        result = _call_kw(role, names=["convertimage"])
        assert result.success
        # Revealed under the canonical (index) casing, not the query casing.
        assert role.revealed == {"ConvertImage"}

    def test_multiple_names_revealed(self):
        role = _role(INDEX)
        _call_kw(role, names=["ConvertImage", "SendEmail"])
        assert role.revealed == {"ConvertImage", "SendEmail"}

    def test_unknown_name_is_error(self):
        role = _role(INDEX)
        with pytest.raises(ToolValidationError, match="Unknown tool name"):
            _call_kw(role, names=["NoSuchTool"])
        assert role.revealed == set()

    def test_names_and_query_unioned(self):
        role = _role(INDEX)
        # names → SendEmail; query "image" → ConvertImage. Union of both.
        result = _call_kw(role, names=["SendEmail"], query="image")
        assert result.success
        assert role.revealed == {"ConvertImage", "SendEmail"}

    def test_names_as_xml_string_split(self):
        # XML delivers ``names`` as a single string (no lists) → comma/space split.
        role = _role(INDEX)
        _call_kw(role, names="ConvertImage, SendEmail")
        assert role.revealed == {"ConvertImage", "SendEmail"}

    def test_neither_query_nor_names_is_error(self):
        role = _role(INDEX)
        with pytest.raises(ToolValidationError) as excinfo:
            _call_kw(role)
        msg = str(excinfo.value)
        assert role.revealed == set()
        assert "query" in msg and "names" in msg

    def test_names_persist_full_description(self):
        role = _role(INDEX)
        role.deferred_descriptions = {"ConvertImage": "Full convert prose."}
        _call_kw(role, names=["ConvertImage"])
        assert "ConvertImage" in role.registered_resources
        kind, content = role.registered_resources["ConvertImage"]
        assert kind == "tool"
        assert content == "Full convert prose."


class TestDeclarations:
    def test_not_reconstructable(self):
        # The result body carries data={"tool_references": …} — the wire
        # projection the Anthropic native path expands into full tool defs — so
        # folding it away would lose those reference blocks. NOT reconstructable
        # (the revealed set still lives durably on RoleState regardless).
        assert SearchTools.reconstructable is False

    def test_requires(self):
        assert SearchTools.requires == (
            "list_deferred_tools",
            "reveal_tools",
            "describe_deferred_tools",
            "register_resource",
        )
