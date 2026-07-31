"""Text and extracted-document view formatting for Read."""

from __future__ import annotations

from mote.contracts.file import FileTextView
from mote.runtime.context.markers import system_reminder

_EMPTY_FILE = "Warning: the file exists but the contents are empty."
_SHORT_FILE = (
    "Warning: the file exists but is shorter than the provided offset " "({offset}). The file has {total} lines."
)
_EMPTY_DOCUMENT = "Warning: the document exists but no text could be extracted."
_SHORT_DOCUMENT = (
    "Warning: the document exists but is shorter than the provided offset "
    "({offset}). The document has {total} lines."
)
_PARTIAL = (
    "This is a partial view. Continue with cursor='{cursor}' to read the next "
    "page from the same immutable snapshot (next position: {offset})."
)


def add_line_numbers(lines: list[str], start_line: int) -> str:
    """Format lines with right-aligned, one-based ``cat -n`` style numbers."""
    output = []
    for index, line in enumerate(lines):
        number = str(index + start_line)
        output.append(f"{number if len(number) >= 6 else number.rjust(6)}→{line}")
    return "\n".join(output)


def format_text_view(view: FileTextView) -> tuple[str, dict]:
    """Format a sealed text view and its model-facing result metadata."""
    is_document = view.mode.value == "document"
    is_empty = not any(view.lines) and view.offset == 1 and view.next_offset is None
    if is_empty:
        output = system_reminder(_EMPTY_DOCUMENT if is_document else _EMPTY_FILE)
    elif not view.lines:
        template = _SHORT_DOCUMENT if is_document else _SHORT_FILE
        output = system_reminder(template.format(offset=view.offset, total=view.total_lines))
    else:
        output = add_line_numbers(list(view.lines), view.offset)
        if view.next_offset is not None:
            output += "\n\n" + system_reminder(_PARTIAL.format(cursor=view.next_cursor, offset=view.next_offset))

    decision = view.snapshot.encoding
    encoding = (
        None
        if decision is None
        else {
            "label": decision.label,
            "source": decision.source.value,
            "confidence": decision.confidence,
            "bom_hex": decision.bom.hex(),
        }
    )
    return output, {
        "type": view.mode.value,
        "line_offset": view.offset,
        "lines_returned": len(view.lines),
        "total_lines": view.total_lines,
        "status": view.status.value,
        "next_offset": view.next_offset,
        "next_cursor": view.next_cursor,
        "encoding": encoding,
        "snapshot_digest": view.snapshot.version.digest,
    }


__all__ = ["add_line_numbers", "format_text_view"]
