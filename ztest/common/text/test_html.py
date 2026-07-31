#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the shared HTML->Markdown purification kernel.

``html_to_markdown`` is the ONE stable ``HTML → Markdown`` contract every
dirty-HTML source (browser ``read``, the curl compressor, a future WebFetch)
converges on. It takes *already-cleaned* HTML and does the conversion plus a
generic post-processing pass (scrub percent-encoding, collapse blank lines).
These are pure-function tests: no browser, no network.
"""
from __future__ import annotations

import pytest

from mote.runtime.media.html import html_to_markdown

# The conversion depends on the optional ``markdownify`` library.
pytest.importorskip("markdownify")


class TestConversion:
    def test_headings_and_paragraphs(self):
        md = html_to_markdown("<h1>Title</h1><p>Body text.</p>")
        assert md is not None
        assert "# Title" in md
        assert "Body text." in md

    def test_div_blocks_not_one_line(self):
        html = "<div><div>First.</div><div>Second.</div></div>"
        md = html_to_markdown(html)
        assert md is not None
        assert "First." in md and "Second." in md
        # The two blocks must not be glued onto a single physical line.
        assert "First.Second." not in md


class TestLinksAndImages:
    def test_links_stripped_by_default(self):
        md = html_to_markdown('<p>See <a href="https://example.com/x">the docs</a>.</p>')
        assert md is not None
        assert "the docs" in md
        assert "https://example.com" not in md  # URL dropped, text kept

    def test_links_kept_when_opted_in(self):
        md = html_to_markdown('<p><a href="https://example.com/x">docs</a></p>', extract_links=True)
        assert md is not None
        assert "https://example.com/x" in md

    def test_images_stripped_by_default(self):
        md = html_to_markdown('<p><img src="https://example.com/a.png" alt="pic"></p>')
        assert md is not None
        assert "example.com" not in md

    def test_images_kept_when_opted_in(self):
        md = html_to_markdown('<img src="https://example.com/a.png" alt="pic">', extract_images=True)
        assert md is not None
        assert "example.com/a.png" in md


class TestPostProcessing:
    def test_percent_encoding_scrubbed(self):
        # A surviving (opted-in) link URL with percent-encoding is scrubbed.
        md = html_to_markdown('<a href="https://x.com/a%20b%2Fc">t</a>', extract_links=True)
        assert md is not None
        assert "%20" not in md and "%2F" not in md

    def test_blank_lines_collapsed(self):
        md = html_to_markdown("<p>a</p><p>b</p><p>c</p>")
        assert md is not None
        assert "\n\n\n" not in md  # never 3+ consecutive newlines

    def test_stripped_edges(self):
        md = html_to_markdown("<p>only</p>")
        assert md is not None
        assert md == md.strip()


class TestDegradation:
    def test_markdownify_absent_returns_none(self, monkeypatch):
        monkeypatch.setattr("mote.runtime.media.html._markdownify", None)
        assert html_to_markdown("<p>x</p>") is None
