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

A third class: a **JSON API response**. A REST endpoint fetched with ``curl``
often returns a large, pretty-printed array of records — exactly the
"high-volume, low-entropy" shape that floods context. We parse it, sample the
dominant array (keep a head + tail of records, elide the middle with a count
marker), and re-serialise minified (whitespace-stripped). This is lossy for the
elided middle but fully reversible: the package persists the raw original to
disk and prepends a pointer, so the model can ``Read`` the exact bytes on
demand — the same contract the HTML path already relies on. Invalid / unparsable
JSON is declined untouched, so we never mangle a non-JSON body.

Anything that is none of HTML / binary / JSON — ``curl -I`` headers, plain
text — is declined (returned unchanged), so a non-page fetch is never mangled.
``markdownify`` / ``bs4`` being optional, an import failure also declines the
HTML path. All of this rides the package's fail-safe + grow-guard wrapper, so a
misdetection can never lose or bloat a tool's output.
"""

from __future__ import annotations

import json
import re

try:
    from bs4 import BeautifulSoup
except Exception:  # noqa: BLE001 — optional HTML cleanup dependency
    BeautifulSoup = None

from mote.runtime.media.html import html_to_markdown
from mote.runtime.media.video import is_url, looks_like_video_path
from mote.runtime.tools.compress.base import CompressionResult, applied, unchanged

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
# so genuine binary arrives as a stream peppered with the
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


# JSON-detection probe: a JSON document begins (after leading whitespace) with an
# object or array opener. Keying on the START keeps a log line like ``[INFO] …``
# out of the parse attempt only weakly — the real gate is ``json.loads`` below,
# which declines anything that is not actually valid JSON.
_JSON_START_RE = re.compile(r"^\s*[\[{]")

# Sampling only kicks in on arrays LONGER than this — a short list is dumped in
# full (minify-only, lossless). Above it we keep a head + tail of records and
# elide the middle, since a 500-record API page is the "low-entropy flood" this
# path exists to tame.
_JSON_SAMPLE_MIN_ITEMS = 20
# How many records to keep from the front / back of a sampled array. The head
# shows the shape + earliest records; the tail preserves the most-recent ones
# (which a blind size-cap truncation would otherwise lose entirely).
_JSON_HEAD_ITEMS = 5
_JSON_TAIL_ITEMS = 2


def _looks_like_json(output: str) -> bool:
    """True when *output* opens like a JSON object/array (final gate is parse)."""
    return bool(_JSON_START_RE.match(output))


def _sample_list(items: list) -> list:
    """Head+tail sample of a long list; short lists pass through untouched.

    The elided middle is replaced by a single marker object carrying the count
    of omitted records, so the result stays valid JSON and the model can see
    exactly how much was dropped (the full array is on disk via the raw pointer).
    """
    if len(items) <= _JSON_SAMPLE_MIN_ITEMS:
        return items
    omitted = len(items) - _JSON_HEAD_ITEMS - _JSON_TAIL_ITEMS
    marker = {"__omitted_items__": omitted}
    return items[:_JSON_HEAD_ITEMS] + [marker] + items[-_JSON_TAIL_ITEMS:]


def _crush_json(data: object) -> object:
    """Sample the dominant array in *data* (one level deep); else pass through.

    A top-level array is sampled directly. A top-level object has its single
    largest list value sampled in place (the ``{"data": [...], "meta": {...}}``
    envelope shape). Anything else is returned unchanged and only benefits from
    the minify step in :func:`_compress_json`.
    """
    if isinstance(data, list):
        return _sample_list(data)
    if isinstance(data, dict):
        biggest_key: str | None = None
        biggest_len = _JSON_SAMPLE_MIN_ITEMS
        for key, value in data.items():
            if isinstance(value, list) and len(value) > biggest_len:
                biggest_key, biggest_len = key, len(value)
        if biggest_key is not None:
            out = dict(data)
            out[biggest_key] = _sample_list(data[biggest_key])  # type: ignore[arg-type]
            return out
    return data


def _compress_json(output: str) -> "str | None":
    """Sample + minify a JSON body; return None to decline (not valid JSON).

    Lossless minification (whitespace-stripped, non-ASCII un-escaped) is the
    baseline; structure-aware sampling of the dominant array is the big win. The
    grow-guard upstream discards the result if it is not actually smaller.
    """
    try:
        data = json.loads(output)
    except (ValueError, RecursionError):
        return None
    crushed = _crush_json(data)
    return json.dumps(crushed, separators=(",", ":"), ensure_ascii=False)


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

        if _looks_like_html(output):
            return self._compress_html(output)

        if _looks_like_json(output):
            compact = _compress_json(output)
            if compact is not None:
                # grow-guard upstream discards this if it is not smaller.
                return applied(output, compact, "curl json")

        # Unparsable JSON / headers / plain text — not compressible. Leave it.
        return unchanged(output, label="curl")

    def _compress_html(self, output: str) -> CompressionResult:
        """Strip structural noise from a page and run the HTML->Markdown kernel."""
        if BeautifulSoup is None:
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
