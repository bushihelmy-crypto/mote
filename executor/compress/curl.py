"""Compressor for ``curl`` / ``wget`` output — the HTML→Markdown purifier.

This is where WebFetch's *purification* half lives (its transport half is just
``curl`` running under the sandbox, which already gives SSRF protection for
free; its "understand" half is unnecessary — an oversized clean Markdown is
handled by the existing result-limit / spill machinery). When a shell fetch
returns a web page, the raw output is a wall of ``<script>`` / ``<style>`` /
nav / ad markup — exactly the "floods the LLM context with low-signal text"
this package exists to fix. We detect HTML, strip the structural noise (a
``bs4`` pass — the Python-side compensation for the visibility powers the
browser has but a headless curl does not), and hand the result to the ONE
shared ``HTML → Markdown`` kernel that ``_browser.py`` also uses.

A second class of low-signal output: a fetch that pulls down **binary or
non-UTF-8 bytes** (an image, a PDF, an archive) straight to stdout. The shell
layer decoded those bytes with ``errors="replace"``, so by the time we see the
output the real bytes are gone — replaced by a wall of ``U+FFFD`` noise that
teaches the model nothing and cannot be recovered. We detect that (high
replacement-char density, or an embedded NUL) and swap the useless blob for a
short, actionable instruction: re-fetch to a FILE (``curl -o``) and open it
with the ``Read`` tool, which is the framework's single media outlet — it
renders an image or PDF to the model directly and extracts text from
documents. We deliberately do NOT try to reconstruct the bytes here (they were
destroyed upstream at decode time); pointing at the correct workflow is the
honest, zero-debt move.

Anything that is neither HTML nor binary — a JSON API response, ``curl -I``
headers, plain text — is declined (returned unchanged), so a JSON fetch is
never mangled. ``markdownify`` / ``bs4`` being optional, an import failure also
declines the HTML path. All of this rides the package's fail-safe + grow-guard
wrapper, so a misdetection can never lose or bloat a tool's output.
"""

from __future__ import annotations

import re

from mote.common.text import html_to_markdown
from mote.executor.compress.base import CompressionResult, applied, unchanged
from mote.executor.dependency._video import is_url, looks_like_video_path

# Tags whose entire subtree is noise for a text reading. Mirrors the browser's
# ``_CLEAN_HTML_JS`` SKIP_TAGS — minus the visibility check (``getComputedStyle``
# needs a live DOM; bs4 has only the static tree, so this is the best-effort
# Python-side equivalent).
_DROP_TAGS = (
    "script",
    "style",
    "noscript",
    "svg",
    "head",
    "nav",
    "footer",
    "aside",
    "header",
    "form",
    "button",
    "input",
    "select",
    "textarea",
    "iframe",
    "template",
)

# HTML-detection probe: after trimming leading whitespace, a page reliably
# starts with a doctype or one of these structural tags. Keying on the START
# (not "contains anywhere") avoids a JSON body that merely quotes "<html>" in a
# string being mistaken for a web page.
_HTML_START_RE = re.compile(r"^\s*(<!doctype\s+html|<html[\s>]|<head[\s>]|<body[\s>])", re.IGNORECASE)

# Binary detection. The shell decoded the fetched bytes with ``errors="replace"``
# (see common.aexecute), so genuine binary arrives as a stream peppered with the
# Unicode replacement char (``U+FFFD``); a raw NUL also never occurs in real text
# output. Either signal, sampled over a bounded prefix, marks the output as an
# undecodable binary blob rather than something worth showing the model.
_REPLACEMENT_CHAR = "\ufffd"
# Fraction of replacement chars over the sampled prefix above which we treat the
# output as binary. A little mojibake in otherwise-real text (a stray byte in a
# UTF-8 page) stays well under this; a decoded image/PDF blows far past it.
_BINARY_REPLACEMENT_RATIO = 0.10
# Only sample the head — enough to classify, cheap on a multi-MB blob.
_BINARY_SAMPLE_CHARS = 4096
# Below this many chars a "binary" verdict is unreliable (a short body can hit a
# high ratio by chance); tiny outputs are left alone.
_BINARY_MIN_CHARS = 64

# A fetched VIDEO is a special binary case worth its own guide: a plain curl/wget
# cannot get a usable video stream (adaptive/segmented delivery, no captions), so
# point at yt-dlp to DOWNLOAD it to a local file, then open that file with Read
# (which decodes a local video into timestamped frames + a transcript).
_VIDEO_NOTICE = (
    "[video response — {chars} chars of undecodable video bytes were dropped by "
    "the shell's text decoding and cannot be recovered here.]\n"
    "This URL is a video ('{url}'). curl/wget cannot fetch a usable video stream. "
    "Download it to a local file with yt-dlp, then open that file with the Read "
    "tool (which decomposes a local video into timestamped frames shown to you as "
    "images plus a timestamped transcript):\n"
    "  yt-dlp -o clip.mp4 <url>\n"
    "then: Read clip.mp4"
)

# The instruction that replaces an undecodable blob: re-fetch to a file, then
# open it with the framework's single media outlet (the Read tool).
_BINARY_NOTICE = (
    "[binary or non-text response — {chars} chars of undecodable bytes were "
    "dropped by the shell's text decoding and cannot be recovered here.]\n"
    "This looks like an image, PDF, archive, or other binary payload streamed "
    "to stdout. To actually use it, fetch it to a FILE and open it with the "
    "Read tool (the framework's media outlet):\n"
    "  curl -L -o <file> <url>   # or: wget -O <file> <url>\n"
    "then: Read <file>\n"
    "Read renders an image or PDF to you directly and extracts text from "
    "documents. Add -I to inspect the response headers (content-type) first."
)


def _video_url(argv: list[str]) -> "str | None":
    """Return the first http(s) URL in *argv* with a video extension, else None.

    Used only to give a fetched video its own yt-dlp-download guide (vs the
    generic "fetch to a file, then Read" binary notice). A bare path check —
    never a network call — so a misdetection can at worst pick the wrong text.
    """
    for arg in argv:
        if is_url(arg) and looks_like_video_path(arg):
            return arg
    return None


def _looks_like_html(output: str) -> bool:
    """True when *output* is a web page (vs a JSON/header/binary fetch)."""
    return bool(_HTML_START_RE.match(output))


def _looks_like_binary(output: str) -> bool:
    """True when *output* is an undecodable binary blob (post-``errors=replace``).

    Sampled over a bounded head: an embedded NUL, or a replacement-char density
    past :data:`_BINARY_REPLACEMENT_RATIO`, means the shell decoded raw bytes
    that no longer carry usable information. Short outputs are never judged
    binary (too little signal to be sure).
    """
    if len(output) < _BINARY_MIN_CHARS:
        return False
    sample = output[:_BINARY_SAMPLE_CHARS]
    if "\x00" in sample:
        return True
    replacements = sample.count(_REPLACEMENT_CHAR)
    return replacements / len(sample) >= _BINARY_REPLACEMENT_RATIO


class CurlCompressor:
    """Turn a fetched HTML page into clean Markdown; decline everything else."""

    prefixes: tuple[str, ...] = ("curl", "wget")

    def compress(self, output: str, *, argv: list[str]) -> CompressionResult:
        if _looks_like_binary(output):
            # Undecodable bytes (image / PDF / archive / video) streamed to
            # stdout — the real payload was already destroyed by the shell's text
            # decoding. Swap the useless blob for a short actionable instruction.
            video = _video_url(argv)
            if video is not None:
                # A video URL: download with yt-dlp to a local file, then Read it.
                notice = _VIDEO_NOTICE.format(chars=len(output), url=video)
            else:
                # Anything else: fetch to a file, then open with the Read outlet.
                notice = _BINARY_NOTICE.format(chars=len(output))
            return applied(output, notice, "curl")

        if not _looks_like_html(output):
            # JSON API / headers / plain text — not a page. Leave it untouched.
            return unchanged(output, label="curl")

        try:
            from bs4 import BeautifulSoup
        except Exception:  # noqa: BLE001 — optional dependency; decline cleanly
            return unchanged(output, label="curl")

        try:
            soup = BeautifulSoup(output, "html.parser")
            for tag in soup.find_all(_DROP_TAGS):
                tag.decompose()
            cleaned = str(soup)
        except Exception:  # noqa: BLE001 — malformed markup; decline cleanly
            return unchanged(output, label="curl")

        md = html_to_markdown(cleaned)
        if not md or not md.strip():
            # Kernel unavailable or produced nothing — leave the original.
            return unchanged(output, label="curl")
        return applied(output, md, "curl")


__all__ = ["CurlCompressor"]
