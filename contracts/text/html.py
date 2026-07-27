"""Deterministic HTML to agent-friendly Markdown projection.

This is the *purification kernel* — one stable, replaceable contract that every
source of dirty HTML converges on. Today it wraps ``markdownify`` plus a small
deterministic post-processing pass (scrub leftover percent-encoding, collapse
blank lines); if the best-practice for main-content extraction later becomes
``trafilatura`` / ``readability``, the implementation can be swapped here with
zero changes at any call site.

Layering (why this lives in ``common/text`` and not ``executor/compress``):
the ``compress`` leaf layer is stdlib-only by contract, but this kernel needs
the optional ``markdownify`` dependency. ``common/text`` is the right home — it
is the bottom-layer text authority (peer to ``strip_ansi`` / ``collapse_whitespace``)
and every layer above may import it. ``markdownify`` is treated as OPTIONAL:
an import failure returns ``None`` so a caller can fall back (mirroring how the
browser's ``read`` already degrades to an ``innerText`` dump).

What this kernel does NOT do — and deliberately: it takes *already-cleaned* HTML.
Source-specific preprocessing (the browser's ``getComputedStyle`` visibility
pass in the live DOM; a ``bs4`` structural strip for a curl fetch) is NOT
shareable — each source cleans with the powers it has — so it stays with each
consumer. The kernel is only the shared, stable ``HTML → Markdown`` step.
"""
from __future__ import annotations

import re
from typing import Optional

try:
    from markdownify import markdownify as _markdownify
except Exception:  # noqa: BLE001 — optional dependency
    _markdownify = None

# Leftover percent-encoding (e.g. ``%20`` inside a surviving link/image URL when
# the caller opted in). Scrubbed to keep output readable — matches browser-use.
_PERCENT_ENCODING_RE = re.compile(r"%[0-9A-Fa-f]{2}")
# Three-or-more consecutive newlines → collapse to a single blank line.
_BLANK_LINES_RE = re.compile(r"\n{3,}")
# Trailing whitespace before a newline.
_TRAILING_WS_RE = re.compile(r"[ \t]+\n")


def html_to_markdown(
    html: str,
    *,
    extract_links: bool = False,
    extract_images: bool = False,
) -> Optional[str]:
    """Convert (already-cleaned) HTML to Markdown via ``markdownify``.

    By default this strips the two biggest sources of noise — images and
    hyperlink URLs — mirroring browser-use's defaults: ``<img>`` is dropped
    entirely and ``<a>`` renders as its plain text (no long percent-encoded
    query-string URLs). Opt back in per-call via ``extract_links`` /
    ``extract_images`` when a URL to navigate to, or an image ``src`` to
    inspect, is actually wanted.

    Args:
        html: An HTML fragment or document. Callers should apply any
            source-specific cleaning (visibility pass, structural strip) first;
            this kernel only does the conversion + generic post-processing.
        extract_links: Keep ``<a>`` hyperlinks (as ``[text](url)``) instead of
            flattening to their text.
        extract_images: Keep ``<img>`` (as ``![alt](src)``) instead of dropping.

    Returns:
        The Markdown string (post-processed, stripped), or ``None`` when
        ``markdownify`` is unavailable or conversion fails — so the caller can
        fall back to a plainer representation.
    """
    if _markdownify is None:
        return None
    # ``strip`` removes a tag's markup while keeping its text, so stripping
    # ``a`` turns "[headline](https://…long%20url…)" into a bare "headline";
    # stripping ``img`` drops decorative images / tracking pixels outright.
    strip = []
    if not extract_images:
        strip.append("img")
    if not extract_links:
        strip.append("a")
    try:
        md = _markdownify(
            html,
            heading_style="ATX",  # '#' style headings
            bullets="-",  # '-' for unordered lists
            escape_asterisks=False,  # cleaner output (don't escape * / _)
            escape_underscores=False,
            escape_misc=False,  # don't escape misc chars (cleaner output)
            autolinks=False,  # don't wrap bare URLs in <>
            default_title=False,  # don't inject default title attrs
            strip=strip or None,
        )
    except Exception:  # noqa: BLE001 — malformed HTML; caller falls back
        return None
    md = _PERCENT_ENCODING_RE.sub("", md)
    md = _BLANK_LINES_RE.sub("\n\n", md)
    md = _TRAILING_WS_RE.sub("\n", md)
    return md.strip()


__all__ = ["html_to_markdown"]
