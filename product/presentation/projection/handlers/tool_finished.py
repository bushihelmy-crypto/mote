"""Finished-tool result, media, artifact, and file-change projection."""

from __future__ import annotations

import os
from typing import Optional

from mote.contracts.events.tool import ToolCallFinishedEvent
from mote.contracts.foundation.errors.report import ErrorReport
from mote.contracts.tool.output_markers import PERSISTED_OUTPUT_OPEN
from mote.product.i18n import keys as K
from mote.product.i18n import t
from mote.product.presentation.events.events import (
    RESULT_KIND_DIFF,
    RESULT_KIND_PLAIN,
    ArtifactBlock,
    FileDiffBlock,
    MediaBlock,
    ToolCallCompleted,
    ViewEvent,
)
from mote.product.presentation.projection.summaries import _result_summary

_MAX_RESULT_CHARS = 200
_MAX_DETAIL_LINES = 40
_MAX_FAILURE_LINES = 5
_MAX_RESULT_WORDS = 100


def _fold_lines(text: str, limit: int) -> tuple[str, int]:
    lines = text.splitlines()
    return (text, 0) if len(lines) <= limit else ("\n".join(lines[:limit]), len(lines) - limit)


def _first_nonempty_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def _preview_words(text: str, limit: int) -> tuple[str, bool]:
    stripped = text.strip("\n")
    words = 0
    in_word = False
    for index, character in enumerate(stripped):
        if character.isspace():
            in_word = False
        elif not in_word:
            in_word = True
            words += 1
            if words > limit:
                return stripped[:index].rstrip(), True
    return stripped, False


def _extract_full_ref(text: str) -> Optional[str]:
    if PERSISTED_OUTPUT_OPEN not in text:
        return None
    marker = "Full output saved to:"
    for line in text.splitlines():
        if marker in line:
            return line.split(marker, 1)[1].strip() or None
    return None


def _looks_like_diff(text: str) -> bool:
    saw_header = False
    for line in text.splitlines():
        if line.startswith("@@") and line.rstrip().endswith("@@"):
            return True
        if line.startswith("--- "):
            saw_header = True
        elif line.startswith("+++ ") and saw_header:
            return True
    return False


def _complete_failed(
    name: str,
    identity,
    text: str,
    full_ref: str | None,
    error: ErrorReport | None,
) -> ToolCallCompleted:
    source = (error.message if error is not None else "").strip() or text
    detail, hidden = _fold_lines(source.strip(), _MAX_FAILURE_LINES)
    truncated = hidden > 0
    if len(detail) > _MAX_RESULT_CHARS:
        detail = detail[:_MAX_RESULT_CHARS] + "…"
        truncated = True
    return ToolCallCompleted(
        tool_name=name,
        ok=False,
        summary=detail,
        identity=identity,
        content_truncated=truncated or full_ref is not None,
        full_ref=full_ref,
        hidden_lines=hidden,
        error_type=error.error if error is not None else "",
        error_code=error.code if error is not None else "",
        retryable=error.retryable if error is not None else False,
        recovery=error.recovery if error is not None else "",
    )


def _complete_tool(event: ToolCallFinishedEvent) -> ToolCallCompleted:
    name = event.tool_name
    identity = event.identity
    response = event.tool_response
    text = response if isinstance(response, str) else ("" if response is None else str(response))
    full_ref = _extract_full_ref(text)
    if event.outcome != "succeeded":
        return _complete_failed(name, identity, text, full_ref, event.error)
    summary = _result_summary(name, event, text) or _first_nonempty_line(text)
    if not summary:
        summary = t(K.RESULT_NO_OUTPUT)
    elif len(summary) > _MAX_RESULT_CHARS:
        summary = summary[:_MAX_RESULT_CHARS] + "…"
    if _looks_like_diff(text):
        detail, hidden = _fold_lines(text.strip(), _MAX_DETAIL_LINES)
        return ToolCallCompleted(
            tool_name=name,
            ok=True,
            summary=summary,
            identity=identity,
            result_kind=RESULT_KIND_DIFF,
            detail=detail,
            lexer="diff",
            content_truncated=hidden > 0 or full_ref is not None,
            full_ref=full_ref,
            hidden_lines=hidden,
        )
    body = text.strip("\n")
    preview, words_truncated = _preview_words(text, _MAX_RESULT_WORDS)
    detail = preview if preview and preview.strip() != summary else None
    hidden = max(0, len(body.splitlines()) - len(preview.splitlines())) if words_truncated and preview else 0
    return ToolCallCompleted(
        tool_name=name,
        ok=True,
        summary=summary,
        identity=identity,
        result_kind=RESULT_KIND_PLAIN,
        detail=detail,
        content_truncated=(
            full_ref is not None or words_truncated or len(body.splitlines()) > 1 or summary.endswith("…")
        ),
        full_ref=full_ref,
        hidden_lines=hidden,
    )


def _media_blocks(event: ToolCallFinishedEvent) -> list[MediaBlock]:
    identity = event.identity
    blocks: list[MediaBlock] = []
    for media in event.media:
        kind = media.kind or "image"
        raw_ref = media.ref
        ref = os.path.abspath(os.path.expanduser(str(raw_ref))) if raw_ref else ""
        blocks.append(
            MediaBlock(
                media_kind=kind,
                ref=ref,
                mime=media.mime,
                artifact=media.artifact,
                alt=(os.path.basename(ref) if ref else "") or kind,
                identity=identity,
            )
        )
    return blocks


def project_tool_finished(event: ToolCallFinishedEvent) -> list[ViewEvent]:
    identity = event.identity
    output: list[ViewEvent] = [_complete_tool(event), *_media_blocks(event)]
    output.extend(ArtifactBlock(artifact=artifact, identity=identity) for artifact in event.artifacts)
    for change in event.file_changes:
        raw_path = change.path
        output.append(
            FileDiffBlock(
                path=os.path.abspath(os.path.expanduser(str(raw_path))) if raw_path else "",
                old=change.old,
                new=change.new,
                identity=identity,
            )
        )
    return output


__all__ = ["project_tool_finished"]
