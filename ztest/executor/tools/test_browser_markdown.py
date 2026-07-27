#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Pure-function tests for the browser's HTML->Markdown conversion.

These do not touch Playwright/Chromium (unlike ``test_web_browser.py``), so they
run everywhere. They lock in the fix for the old hand-rolled walker's
"everything collapses to one line" bug on div/section-based pages: the walker
only emitted newlines for a whitelist of semantic tags, so modern div-based
layouts rendered as a single line. ``markdownify`` lays out all block-level
elements across proper lines.
"""
from __future__ import annotations

import pytest

from mote.runtime.tools.dependency._browser import _html_to_markdown

# The conversion depends on the optional ``markdownify`` library.
pytest.importorskip("markdownify")


def test_div_based_page_is_not_one_line():
    """div/section blocks (no <p>/<h*>) must render across multiple lines."""
    html = (
        "<div>" "<div>First block of text.</div>" "<div>Second block of text.</div>" "<div>Third block.</div>" "</div>"
    )
    md = _html_to_markdown(html)
    assert md is not None
    # All three blocks present, and not glued onto one physical line.
    assert "First block of text." in md
    assert "Second block of text." in md
    assert "Third block." in md
    assert md.count("\n") >= 2


def test_headings_and_paragraphs_render_as_markdown():
    html = "<h1>Big Title</h1><p>First paragraph of the body.</p>" "<h2>Section</h2><p>Second paragraph here.</p>"
    md = _html_to_markdown(html)
    assert md is not None
    assert "# Big Title" in md
    assert "## Section" in md
    assert "First paragraph of the body." in md
    assert "Second paragraph here." in md


def test_lists_render_as_markdown():
    html = "<ul><li>Apple</li><li>Banana</li></ul>"
    md = _html_to_markdown(html)
    assert md is not None
    assert "- Apple" in md
    assert "- Banana" in md


def test_links_dropped_to_plain_text_by_default():
    """Default (extract_links=False): links render as bare text, no URL noise."""
    html = "<a href='/a'>Home</a>"
    md = _html_to_markdown(html)
    assert md is not None
    assert "Home" in md
    assert "/a" not in md
    assert "[Home]" not in md


def test_links_kept_when_extract_links_true():
    html = "<a href='/a'>Home</a>"
    md = _html_to_markdown(html, extract_links=True)
    assert md is not None
    assert "[Home](/a)" in md


def test_images_dropped_by_default():
    """Default (extract_images=False): <img> produces no output."""
    html = "<p>before</p><img src='//cdn.example.com/pixel.png' alt='x'><p>after</p>"
    md = _html_to_markdown(html)
    assert md is not None
    assert "before" in md
    assert "after" in md
    assert "pixel.png" not in md
    assert "![" not in md


def test_images_kept_when_extract_images_true():
    html = "<img src='//cdn.example.com/logo.png' alt='logo'>"
    md = _html_to_markdown(html, extract_images=True)
    assert md is not None
    assert "logo.png" in md


def test_percent_encoding_scrubbed_from_kept_links():
    """When links are kept, leftover %XX percent-encoding is scrubbed."""
    html = "<a href='https://ex.com/s?wd=%E5%90%91'>headline</a>"
    md = _html_to_markdown(html, extract_links=True)
    assert md is not None
    assert "%E5" not in md
    assert "%90" not in md
    assert "headline" in md


def test_blank_lines_collapsed():
    """3+ consecutive blank lines are collapsed to a single blank line."""
    md = _html_to_markdown("<p>a</p><p>b</p><p>c</p>")
    assert md is not None
    assert "\n\n\n" not in md
    assert not md.startswith("\n")
    assert not md.endswith("\n")
