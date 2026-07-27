#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the ``curl`` / ``wget`` HTML->Markdown compressor.

CurlCompressor is where WebFetch's *purification* half lives: a shell fetch of
a web page returns a wall of ``<script>``/``<style>``/nav markup; this detects
HTML, strips the structural noise (bs4), and runs the shared HTML->Markdown
kernel. Anything that is NOT a page — a JSON API response, ``curl -I`` headers,
a binary blob — is declined (returned unchanged). Everything rides the
package's fail-safe + grow-guard wrapper.
"""
from __future__ import annotations

import json

import pytest

from mote.runtime.tools.compress import compress_output
from mote.runtime.tools.compress.curl import CurlCompressor, _looks_like_binary, _looks_like_html, _looks_like_json

# Conversion depends on the optional markdownify/bs4 stack.
pytest.importorskip("markdownify")
pytest.importorskip("bs4")


# A noisy HTML page: real content buried under script/style/nav. Repeated to
# clear the compression floor and to make the drop-tags removal measurable.
_NOISE = "<script>var x=1;alert('tracking');</script><style>.a{color:red}</style>"
_PAGE = (
    "<!doctype html><html><head><title>T</title>" + _NOISE + "</head>"
    "<body><nav>" + ("menu item " * 50) + "</nav>"
    "<h1>Real Heading</h1><p>" + ("The actual article body. " * 40) + "</p>"
    "<footer>" + ("copyright boilerplate " * 50) + "</footer></body></html>"
)


class TestHtmlDetection:
    def test_doctype_is_html(self):
        assert _looks_like_html("<!DOCTYPE html><html></html>")

    def test_html_tag_is_html(self):
        assert _looks_like_html("  \n<html><body>x</body></html>")

    def test_json_is_not_html(self):
        assert not _looks_like_html('{"status": "ok", "note": "<html> in a string"}')

    def test_headers_not_html(self):
        assert not _looks_like_html("HTTP/2 200\ncontent-type: text/html\n")


# A decoded binary blob: what the shell hands us after `errors="replace"` turned
# an image/PDF's bytes into a wall of the Unicode replacement char. Made large so
# it clears the grow-guard against the (short) replacement notice.
_FFFD = "\ufffd"
_BINARY_BLOB = ("\ufffdPNG\r\n" + _FFFD * 20 + "IHDR" + _FFFD * 30) * 200


class TestBinaryDetection:
    def test_replacement_char_wall_is_binary(self):
        assert _looks_like_binary(_BINARY_BLOB)

    def test_embedded_nul_is_binary(self):
        assert _looks_like_binary("some header\x00" + "x" * 100)

    def test_plain_text_is_not_binary(self):
        assert not _looks_like_binary("just some plaintext output\n" * 100)

    def test_html_is_not_binary(self):
        assert not _looks_like_binary(_PAGE)

    def test_json_is_not_binary(self):
        assert not _looks_like_binary('{"status": "ok", "items": [1, 2, 3]}' * 20)

    def test_tiny_output_never_binary(self):
        # Too short to judge — a stray replacement char must not trip it.
        assert not _looks_like_binary(_FFFD * 3)

    def test_sparse_mojibake_stays_text(self):
        # One bad byte in an otherwise-real UTF-8 body is under the ratio, so it
        # is kept as text (declined), not swapped for the binary notice.
        assert not _looks_like_binary("a real sentence with one bad byte " + _FFFD + " and more text " * 20)


class TestBinaryCompress:
    def test_binary_blob_swapped_for_notice(self):
        r = CurlCompressor().compress(_BINARY_BLOB, argv=["curl", "https://x.com/img.png"])
        assert r.applied is True
        # The useless blob is gone; the actionable workflow is in.
        assert _FFFD not in r.text
        assert "curl -L -o" in r.text
        assert "Read" in r.text
        assert r.compressed_chars < r.original_chars

    def test_notice_reports_dropped_size(self):
        r = CurlCompressor().compress(_BINARY_BLOB, argv=["curl", "https://x.com/img.png"])
        assert str(len(_BINARY_BLOB)) in r.text

    def test_binary_checked_before_html(self):
        # A blob that happens to start with an <html-ish byte run but is really
        # binary must still be caught by the binary branch first (NUL present).
        blob = "<html>\x00" + _FFFD * 500
        r = CurlCompressor().compress(blob, argv=["curl", "https://x.com"])
        assert r.applied is True
        assert "curl -L -o" in r.text

    def test_routed_via_compress_output(self):
        r = compress_output("curl https://x.com/img.png", _BINARY_BLOB, min_chars=1, max_input_chars=2_000_000)
        assert r.applied is True
        assert "Read" in r.text

    def test_routed_via_wget(self):
        r = compress_output("wget -O- https://x.com/doc.pdf", _BINARY_BLOB, min_chars=1, max_input_chars=2_000_000)
        assert r.applied is True
        assert "curl -L -o" in r.text


class TestVideoRecognition:
    def test_video_url_guides_to_ytdlp_and_read(self):
        # A fetched video blob whose URL has a video extension gets the yt-dlp
        # download guide (not the generic "fetch to a file, then Read" notice).
        r = CurlCompressor().compress(_BINARY_BLOB, argv=["curl", "https://x.com/clip.mp4"])
        assert r.applied is True
        assert "yt-dlp" in r.text
        assert "Read" in r.text
        assert "https://x.com/clip.mp4" in r.text

    def test_non_video_binary_uses_generic_notice(self):
        # A non-video binary URL keeps the generic Read-outlet workflow.
        r = CurlCompressor().compress(_BINARY_BLOB, argv=["curl", "https://x.com/img.png"])
        assert r.applied is True
        assert "curl -L -o" in r.text
        assert "yt-dlp" not in r.text


class TestCompressApplied:
    def test_html_page_converted_to_markdown(self):
        r = CurlCompressor().compress(_PAGE, argv=["curl", "https://x.com"])
        assert r.applied is True
        assert "# Real Heading" in r.text
        assert "The actual article body." in r.text
        # Noise is gone.
        assert "alert('tracking')" not in r.text
        assert "color:red" not in r.text
        assert "copyright boilerplate" not in r.text

    def test_smaller_than_original(self):
        r = CurlCompressor().compress(_PAGE, argv=["curl", "https://x.com"])
        assert r.applied is True
        assert r.compressed_chars < r.original_chars


class TestJsonDetection:
    def test_object_is_json(self):
        assert _looks_like_json('  {"a": 1}')

    def test_array_is_json(self):
        assert _looks_like_json("\n[1, 2, 3]")

    def test_html_is_not_json(self):
        assert not _looks_like_json("<!doctype html><html></html>")

    def test_headers_are_not_json(self):
        assert not _looks_like_json("HTTP/2 200\ncontent-type: application/json\n")


class TestJsonCompress:
    def test_large_array_sampled(self):
        # A dict with one big list (the {"data": [...], "meta": ...} shape) is
        # sampled: head + tail records kept, the middle elided with a count.
        body = '{"items": [' + ",".join(f'{{"id": {i}}}' for i in range(200)) + "]}"
        r = CurlCompressor().compress(body, argv=["curl", "https://api.x.com"])
        assert r.applied is True
        assert r.compressed_chars < r.original_chars
        parsed = json.loads(r.text)
        items = parsed["items"]
        # 5 head + 1 marker + 2 tail == 8, with the marker naming the omission.
        assert len(items) == 8
        assert items[0] == {"id": 0}
        assert items[-1] == {"id": 199}
        assert {"__omitted_items__": 193} in items

    def test_top_level_array_sampled(self):
        body = json.dumps([{"n": i} for i in range(100)])
        r = CurlCompressor().compress(body, argv=["curl", "https://api.x.com"])
        assert r.applied is True
        parsed = json.loads(r.text)
        assert parsed[0] == {"n": 0}
        assert parsed[-1] == {"n": 99}
        assert {"__omitted_items__": 93} in parsed

    def test_pretty_json_minified_losslessly(self):
        # A small pretty-printed object is not sampled — only minified. The
        # value survives round-trip exactly; only whitespace is removed.
        original = {"status": "ok", "count": 3, "nested": {"a": [1, 2, 3]}}
        body = json.dumps(original, indent=4)
        r = CurlCompressor().compress(body, argv=["curl", "https://api.x.com"])
        assert r.applied is True
        assert r.compressed_chars < r.original_chars
        assert json.loads(r.text) == original

    def test_short_array_not_sampled(self):
        # Below the sampling floor: minified but every record kept.
        original = [{"id": i} for i in range(5)]
        body = json.dumps(original, indent=2)
        r = CurlCompressor().compress(body, argv=["curl", "https://api.x.com"])
        assert r.applied is True
        assert json.loads(r.text) == original

    def test_already_compact_json_declined_by_grow_guard(self):
        # Minifying an already-minified small body saves nothing -> grow-guard
        # collapses it to unchanged (via the package wrapper).
        body = '{"a":1,"b":2}'
        r = compress_output("curl https://x.com", body, min_chars=1, max_input_chars=2_000_000)
        assert r.applied is False
        assert r.text == body

    def test_invalid_json_declined(self):
        # Opens like JSON ('[') but is not valid -> parse fails, left untouched.
        body = "[INFO] starting\n[INFO] processing\n[INFO] done\n" * 20
        r = CurlCompressor().compress(body, argv=["curl", "https://x.com/log"])
        assert r.applied is False
        assert r.text == body

    def test_routed_via_compress_output(self):
        body = '{"items": [' + ",".join(f'{{"id": {i}}}' for i in range(200)) + "]}"
        r = compress_output("curl https://api.x.com", body, min_chars=100, max_input_chars=2_000_000)
        assert r.applied is True
        assert r.compressed_chars < r.original_chars
        assert json.loads(r.text)["items"][0] == {"id": 0}


class TestDecline:
    def test_headers_declined(self):
        body = "HTTP/2 200\n" + "".join(f"x-header-{i}: value\n" for i in range(100))
        r = CurlCompressor().compress(body, argv=["curl", "-I", "https://x.com"])
        assert r.applied is False
        assert r.text == body

    def test_plain_text_declined(self):
        body = "just some plaintext output\n" * 100
        r = CurlCompressor().compress(body, argv=["curl", "https://x.com/robots.txt"])
        assert r.applied is False
        assert r.text == body


class TestWiring:
    def test_routed_via_compress_output_curl(self):
        r = compress_output("curl https://x.com", _PAGE, min_chars=100, max_input_chars=2_000_000)
        assert r.applied is True
        assert "# Real Heading" in r.text

    def test_routed_via_compress_output_wget(self):
        r = compress_output("wget -qO- https://x.com", _PAGE, min_chars=100, max_input_chars=2_000_000)
        assert r.applied is True
        assert "# Real Heading" in r.text

    def test_even_tiny_html_compresses(self):
        # HTML tags are pure overhead, so even a tiny page shrinks once the
        # markup is stripped — markdownify turns it into bare text.
        tiny = "<html><body><p>hi</p></body></html>"
        r = compress_output("curl https://x.com", tiny, min_chars=1, max_input_chars=2_000_000)
        assert r.applied is True
        assert r.compressed_chars < r.original_chars
        assert "hi" in r.text
