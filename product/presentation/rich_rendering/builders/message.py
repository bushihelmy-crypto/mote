"""User-message, URL, and conversation-compaction renderables."""

from __future__ import annotations

import re

from rich.style import Style

from mote.product.i18n import keys as K
from mote.product.i18n import t
from mote.product.presentation.events import ConversationCompacted
from mote.product.presentation.rich_rendering.builders._rich import Text
from mote.product.presentation.rich_rendering.builders.common import bullet_row
from mote.product.presentation.rich_rendering.palette import COMPACT, PROMPT_SYMBOL, Palette

_URL_RE = re.compile(r"https?://[^\s<>\"'`]+")
_URL_TRAILING = ")]}>,.;:!?\"'`"
_COMPACT_SUMMARY_MAX_LINES = 12


def user_message_row(text: str):
    return bullet_row(PROMPT_SYMBOL, Text(text), style=f"bold {Palette.BRAND}")


def _split_url(match: str) -> tuple[str, str]:
    trailing = ""
    while match and match[-1] in _URL_TRAILING:
        if match[-1] == ")" and match.count(")") <= match.count("("):
            break
        trailing = match[-1] + trailing
        match = match[:-1]
    return match, trailing


def linkify(text: str, *, base_style: str = "") -> "Text":
    output = Text()
    index = 0
    for match in _URL_RE.finditer(text):
        start, end = match.span()
        if start > index:
            output.append(text[index:start], style=base_style)
        url, trailing = _split_url(match.group())
        output.append(
            url,
            style=Style.parse(f"underline {Palette.LINK}") + Style(link=url),
        )
        if trailing:
            output.append(trailing, style=base_style)
        index = end
    if index < len(text):
        output.append(text[index:], style=base_style)
    return output


def conversation_compacted_text(event: ConversationCompacted) -> "Text":
    count = event.message_count or 0
    line = Text()
    line.append(f"{COMPACT} " + t(K.COMPACT_COMPACTED), style=Palette.DIM)
    if count:
        line.append(f" ({t(K.COMPACT_KEPT, count=count)})", style=Palette.DIM)
    return line


def compaction_summary_text(
    summary: str,
    *,
    max_lines: int = _COMPACT_SUMMARY_MAX_LINES,
) -> "Text":
    text = Text()
    lines = (summary or "").strip().splitlines()
    if not lines:
        return text
    shown = lines[:max_lines]
    for index, line in enumerate(shown):
        if index:
            text.append("\n")
        text.append(line, style=Palette.DIM)
    hidden = len(lines) - len(shown)
    if hidden > 0:
        text.append("\n" + t(K.FOLD_MORE_LINES, count=hidden), style=Palette.DIM)
    return text


__all__ = [
    "compaction_summary_text",
    "conversation_compacted_text",
    "linkify",
    "user_message_row",
]
