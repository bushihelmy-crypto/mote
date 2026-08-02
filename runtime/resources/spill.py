"""Neutral oversized-resource spill and inline-preview service.

When a single tool produces output larger than its cap, the output is not
silently dropped: the *full* output is written to a session-scoped file on disk
and the inline content is replaced with a short ``<persisted-output>`` preview
that names the file. The model keeps a pointer to the full result and a leading
slice, instead of a blunt mid-string truncation.

This module handles the single-tool path (persist + build message + generate
preview). The per-*message* aggregate budget is a separate feature and is
not handled here.

Used by tool execution and context compaction through one neutral runtime
service. The config data model
:class:`ToolResultLimitConfig` is NOT composed by ``ContextManagerConfig`` (that
config's own docstring is explicit: the tool-execution scope is owned
elsewhere). It is configured under the ``tools`` group and owned by the
``ToolExecutor``; the compaction spill reducer borrows that one instance back
off the built executor, so there is a single source with no drift.

Disk writes go through :mod:`mote.runtime.persistence.atomic` so this shares the exact
read/write primitives with ``tasks/disk_output.py`` (per the project decision
to factor common disk I/O into one place). The on-disk *location* is resolved
exclusively through :class:`mote.runtime.session.workspace.SessionWorkspace`, so a
persisted result co-locates under its session directory alongside the rollout,
so both are swept together with the session.
"""

from __future__ import annotations

from pathlib import Path

from mote.contracts.config.tool import (
    DEFAULT_MAX_RESULT_SIZE_CHARS,
    PERSISTED_OUTPUT_CLOSE_TAG,
    PERSISTED_OUTPUT_OPEN_TAG,
    PREVIEW_SIZE_BYTES,
)
from mote.contracts.ports.task.operations import TaskOutputLocationPort
from mote.contracts.session.identity import SessionId
from mote.runtime.persistence import disk_io
from mote.runtime.resources.formatting import format_file_size
from mote.runtime.telemetry.logging import logger
from mote.runtime.text.elision import cap_head


def persistence_threshold(declared_max_result_size_chars: int) -> int:
    """Effective persist threshold for a tool.

    A tool's own declared cap, clamped by the system-wide default. ``inf``
    is a hard opt-out for tools that bound themselves by other means.
    """
    if declared_max_result_size_chars == float("inf"):
        return declared_max_result_size_chars
    return min(declared_max_result_size_chars, DEFAULT_MAX_RESULT_SIZE_CHARS)


def generate_preview(content: str, max_bytes: int) -> tuple[str, bool]:
    """Return ``(preview, has_more)``.

    Truncates at the last newline within *max_bytes* when that newline lands
    past the halfway point (avoids cutting mid-line); otherwise cuts at the
    exact byte limit.
    """
    if len(content) <= max_bytes:
        return content, False
    truncated = content[:max_bytes]
    last_newline = truncated.rfind("\n")
    cut_point = last_newline if last_newline > max_bytes * 0.5 else max_bytes
    return content[:cut_point], True


def _tool_result_path(session_id: str, result_id: str, store: TaskOutputLocationPort | None) -> Path:
    """Filepath where a tool result is persisted (``{id}.txt``).

    Location comes from the :class:`SessionWorkspace` — the ``tool_results/`` space
    under *session_id*'s directory — never computed here, so the layout stays
    single-sourced.
    """
    if store is None:
        raise ValueError("tool-result persistence requires an injected location owner")
    return store.tool_result_path(SessionId(session_id), result_id)


def _build_persisted_message(filepath: str, original_size: int, preview: str, has_more: bool) -> str:
    """Wrap a preview in the ``<persisted-output>`` envelope."""
    message = f"{PERSISTED_OUTPUT_OPEN_TAG}\n"
    message += f"Output too large ({format_file_size(original_size)}). Full output saved to: {filepath}\n\n"
    message += f"Preview (first {format_file_size(PREVIEW_SIZE_BYTES)}):\n"
    message += preview
    message += "\n...\n" if has_more else "\n"
    message += PERSISTED_OUTPUT_CLOSE_TAG
    return message


def persist_result(output: str, result_id: str, session_id: str, store: TaskOutputLocationPort | None) -> str | None:
    """Write *output* to disk; return its filepath, or None on failure.

    Idempotent: a result id is unique per invocation and its content
    deterministic, so an already-written file is reused rather than rewritten
    (keeps repeated turns byte-identical). Public so the compression layer can
    stash a full original under its own id namespace via the same primitive.
    """
    path = _tool_result_path(session_id, result_id, store)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            disk_io.write_bytes(path, output.encode("utf-8"), append=False)
        return str(path)
    except OSError as e:
        logger.warning(f"Failed to persist tool result {path}: {e}")
        return None


def _truncate_with_notice(output: str, threshold: int) -> str:
    """Plain head-truncation + annotation (persistence disabled path).

    Keeps a leading slice at a newline boundary (via ``generate_preview``) and
    appends how much was dropped, so the model knows the result is incomplete.
    """
    preview, _ = generate_preview(output, threshold)
    _, el = cap_head(output, len(preview))
    if el is None:
        return preview
    marker = el.render_for_model(noun="", format_count=format_file_size, with_total=True)
    return f"{preview}\n\n{marker}"


def enforce_tool_result_limit(
    output: str,
    tool_name: str,
    *,
    result_id: str,
    session_id: str = "",
    max_result_size_chars: int = DEFAULT_MAX_RESULT_SIZE_CHARS,
    persist: bool = True,
    store: TaskOutputLocationPort | None = None,
) -> str:
    """Cap a single tool's *output*, persisting the full result when too large.

    Persists a large result for the per-tool path:

    - Output at/under the effective threshold is returned unchanged.
    - Already-persisted output (starts with ``<persisted-output>``) is left
      alone so re-enforcement is a no-op (prefix-stable across turns).
    - Over threshold with ``persist=True``: the full output is written to a
      session-scoped file and replaced by a ``<persisted-output>`` preview.
    - Over threshold with ``persist=False`` (or a persist failure): the output
      is head-truncated and annotated with the dropped size.

    Args:
        output: The tool's raw text output.
        tool_name: Tool name (currently only used for logging).
        result_id: Stable id for the result (e.g. the tool-use id); names the
            on-disk file and makes persistence idempotent.
        session_id: Owning session; scopes the on-disk path.
        max_result_size_chars: The tool's declared cap (clamped by the
            system-wide default via :func:`persistence_threshold`).
        persist: Persist to disk when True; otherwise truncate inline.
        store: Workspace layout owner that resolves the on-disk location
            (defaults to the standard workspace root).
    """
    if not output:
        return output

    threshold = persistence_threshold(max_result_size_chars)
    if len(output) <= threshold:
        return output

    # Idempotent: don't re-wrap content we already wrapped.
    if output.startswith(PERSISTED_OUTPUT_OPEN_TAG):
        return output

    if persist:
        filepath = persist_result(output, result_id, session_id, store)
        if filepath is not None:
            preview, has_more = generate_preview(output, PREVIEW_SIZE_BYTES)
            return _build_persisted_message(filepath, len(output), preview, has_more)

    # Persistence disabled or failed — fall back to inline truncation.
    return _truncate_with_notice(output, threshold)
