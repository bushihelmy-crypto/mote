"""Tool-call, result, file, media, and task-progress renderables."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, List, Optional, Protocol

from mote.product.i18n import keys as K
from mote.product.i18n import t
from mote.product.presentation.events import (
    GROUP_READ_TOOLS,
    GROUP_SEARCH_TOOLS,
    RESULT_KIND_DIFF,
    RESULT_KIND_TABLE,
    FileDiffBlock,
    FoldMode,
    MediaBlock,
    TaskProgress,
    ToolCallCompleted,
    ToolCallStarted,
    fold_mode,
)
from mote.product.presentation.rich_rendering.builders._rich import Syntax, Table, Text, box
from mote.product.presentation.rich_rendering.builders.common import RESULT_INDENT, indent
from mote.product.presentation.rich_rendering.builders.diff import render_diff
from mote.product.presentation.rich_rendering.builders.message import linkify
from mote.product.presentation.rich_rendering.palette import (
    BRANCH,
    BULLET,
    CHECK,
    CROSS,
    MEDIA,
    PLAY,
    SCISSORS,
    SKIP,
    Palette,
)

_REJECTION_CODE = "TOOL_PERMISSION_DENIED"
_TASK_PROGRESS_STYLE = {
    "running": (PLAY, Palette.BRAND),
    "success": (CHECK, Palette.SUCCESS),
    "failed": (CROSS, Palette.ERROR),
}


def build_table(tsv: str) -> Optional["Table"]:
    rows = [line.split("\t") for line in tsv.splitlines() if line.strip()]
    if not rows:
        return None
    table = Table(show_header=True, header_style=f"bold {Palette.BRAND}", box=box.SIMPLE)
    for column in rows[0]:
        table.add_column(column)
    for row in rows[1:]:
        table.add_row(*row)
    return table


class FoldNoteSource(Protocol):
    full_ref: Optional[str]
    hidden_lines: int


def tool_body_syntax(event: ToolCallStarted) -> Optional["Syntax"]:
    if not event.body:
        return None
    return Syntax(
        event.body,
        event.lexer or "text",
        theme="ansi_dark",
        word_wrap=True,
        background_color="default",
    )


def is_rejection(event: ToolCallCompleted | None) -> bool:
    return bool(event is not None and not event.ok and (event.error_code or "") == _REJECTION_CODE)


def tool_started_text(
    event: ToolCallStarted,
    *,
    ok: Optional[bool] = None,
    blink: bool = False,
    rejected: bool = False,
) -> "Text":
    if ok is None:
        bullet_style = Palette.SHIMMER if blink else Palette.BRAND
    elif rejected:
        bullet_style = Palette.WARNING
    else:
        bullet_style = Palette.SUCCESS if ok else Palette.ERROR
    line = Text()
    line.append(BULLET + " ", style=bullet_style)
    line.append(event.title or event.tool_name, style=f"bold {Palette.BRAND}")
    if event.headline:
        line.append("(", style=Palette.DIM)
        line.append(event.headline, style=Palette.DIM)
        line.append(")", style=Palette.DIM)
    return line


def tool_completed_text(event: ToolCallCompleted) -> "Text":
    line = Text()
    line.append("  " + BRANCH + " ", style=Palette.DIM)
    if is_rejection(event):
        line.append(t(K.TOOL_REJECTED), style=Palette.WARNING)
        return line
    line.append(
        event.summary or (t(K.RESULT_NO_OUTPUT) if event.ok else t(K.RESULT_FAILED)),
        style=Palette.SUCCESS if event.ok else Palette.ERROR,
    )
    error_type = event.error_type or ""
    if not event.ok and error_type:
        suffix = f" [{error_type}]"
        if event.retryable:
            suffix += f" · {t(K.RESULT_RETRYABLE)}"
        line.append(suffix, style=Palette.DIM)
    return line


def render_result_detail(event: ToolCallCompleted, spaces: int = RESULT_INDENT) -> List[Any]:
    detail = event.detail
    if not detail:
        return []
    kind = event.result_kind
    if kind == RESULT_KIND_DIFF:
        return [indent(render_diff(detail), spaces)]
    if kind == RESULT_KIND_TABLE:
        table = build_table(detail)
        return [indent(table, spaces)] if table is not None else []
    return [indent(linkify(detail, base_style=Palette.DIM), spaces)]


def fold_note_str(event: FoldNoteSource) -> str:
    full_ref = event.full_ref
    hidden = event.hidden_lines or 0
    if full_ref:
        return f"{SCISSORS} " + t(K.FOLD_FULL_REF, ref=full_ref)
    if hidden > 0:
        return t(K.FOLD_HIDDEN_LINES, count=hidden)
    return t(K.FOLD_CONTENT)


def fold_note(event: FoldNoteSource) -> "Text":
    return Text(fold_note_str(event), style=Palette.DIM)


def file_change_verb(old: str, new: str) -> str:
    return "created" if not old else ("deleted" if not new else "updated")


def file_change_caption(event: FileDiffBlock) -> "Text":
    old = event.old or ""
    new = event.new or ""
    path = event.path or ""
    caption = Text()
    caption.append("  " + BRANCH + " ", style=Palette.DIM)
    caption.append(f"{path or 'file'} ", style=Palette.BRAND)
    caption.append(f"({file_change_verb(old, new)})", style=Palette.DIM)
    return caption


def media_caption(event: MediaBlock) -> "Text":
    label = event.media_kind or "media"
    reference = event.ref or event.alt or "(no reference)"
    caption = Text()
    caption.append("  " + BRANCH + " ", style=Palette.DIM)
    caption.append(f"{MEDIA} [{label}] ", style=Palette.BRAND)
    caption.append(reference, style=Palette.DIM)
    return caption


def task_progress_text(event: TaskProgress) -> "Text":
    status = event.status
    symbol, style = _TASK_PROGRESS_STYLE.get(status, (SKIP, Palette.WARNING))
    line = Text()
    line.append("  " + symbol + " ", style=style)
    line.append(f"{event.stage or '?'} {status}", style=style)
    detail = event.detail
    if detail and status == "failed":
        line.append(f": {detail}", style=Palette.DIM)
    return line


def tool_group_summary_text(items: Sequence[tuple[str, str]], *, active: bool) -> "Text":
    text = Text()
    if not items:
        return text
    search = sum(1 for name, _ in items if name in GROUP_SEARCH_TOOLS)
    read_paths = {path for name, path in items if name in GROUP_READ_TOOLS and path}
    read = len(read_paths) + sum(1 for name, path in items if name in GROUP_READ_TOOLS and not path)
    parts: list[str] = []
    if search:
        parts.append(t(K.GROUP_SEARCH, count=search))
    if read:
        parts.append(t(K.GROUP_READ, count=read))
    if not parts:
        return text
    text.append(BULLET + " ", style=Palette.BRAND)
    text.append(t(K.LIST_SEP).join(parts), style=Palette.DIM)
    if active:
        text.append("…", style=Palette.DIM)
    return text


__all__ = [
    "FoldMode",
    "build_table",
    "file_change_caption",
    "file_change_verb",
    "fold_mode",
    "fold_note",
    "fold_note_str",
    "is_rejection",
    "media_caption",
    "render_result_detail",
    "task_progress_text",
    "tool_body_syntax",
    "tool_completed_text",
    "tool_group_summary_text",
    "tool_started_text",
]
