#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Logging core: the loguru ``logger`` instance and its sink configuration.

The file sink gains rotation / retention / enqueue (vs. the legacy ``logs.py``)
and every record is stamped with the current ``trace_id`` via a patcher. The
patcher imports :mod:`mote.common.logs.context` lazily so this module never
participates in an import cycle (``context`` is a zero-dependency leaf).
"""

from __future__ import annotations

import os
import sys
from typing import Any, Optional

from loguru import logger as _logger

from mote.common.const import CONFIG_ROOT
from mote.common.logs.context import current_trace_id

# Multi-process/thread-safe writes + rotation + bounded retention.
_FILE_LOG_KWARGS: dict[str, Any] = dict(rotation="50 MB", retention="14 days", enqueue=True)

_LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "{extra[trace_id]}"
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)


def _trace_patcher(record) -> None:
    """Stamp each record with the current trace_id (empty string when unbound)."""
    # Lazy import breaks any potential core <-> context import cycle.

    tid = current_trace_id()
    record["extra"]["trace_id"] = f"[{tid}] " if tid else ""


# The id + level of the live stderr sink, so it can be suspended/resumed precisely
# (e.g. while an interactive REPL owns stdout and must not interleave log lines).
_console_sink_id: Optional[int] = None
_console_level: str = "INFO"

# Per-session file sinks, keyed by session_id -> loguru sink id, so a session's
# sink can be removed when the session ends (a single CLI process opens many
# sessions via /new · /resume · /fork · sub-agents, so leaking sinks would leak
# file handles). Populated by bind_session_logfile / drained by unbind.
_session_sink_ids: dict[str, int] = {}


def define_log_level(print_level: str = "INFO", logfile_level: str = "DEBUG", name: Optional[str] = None):
    """(Re)configure the loguru logger.

    Args:
        print_level: Minimum level routed to stderr.
        logfile_level: Minimum level routed to the dated log file.
        name: Optional prefix for the log file name.

    Returns:
        The configured loguru logger.
    """
    global _console_sink_id, _console_level
    _logger.remove()
    _session_sink_ids.clear()
    # configure() is set every call so an external configure() can't drop the patcher.
    _logger.configure(patcher=_trace_patcher)
    _console_sink_id = _logger.add(sys.stderr, level=print_level, format=_LOG_FORMAT)
    _console_level = print_level
    # File logging is now per-session (one ``logs/{session_id}.txt`` aligned with
    # the workspace session folder), added lazily via ``bind_session_logfile``
    # when a session starts — see that function. No global date-based file sink.
    return _logger


def bind_session_logfile(session_id: str, level: str = "DEBUG") -> Optional[int]:
    """Add a per-session file sink at ``logs/{session_id}.txt``.

    Each session gets its own log file named to match its workspace session
    folder (``{workspace}/.agent_sessions/{session_id}``). The sink is filtered
    to records whose bound trace_id equals *session_id* — ``Role.run`` binds the
    ``session_id`` as the trace_id (see ``bind_trace``), so every line emitted
    during that session (loop / think / executor / tools) lands here.

    Idempotent per session (returns the existing sink id on a repeat call) and a
    no-op when file logging is disabled via ``MOTE_DISABLE_FILE_LOG``.
    """
    if os.getenv("MOTE_DISABLE_FILE_LOG"):
        return None
    existing = _session_sink_ids.get(session_id)
    if existing is not None:
        return existing

    def _only_this_session(record) -> bool:
        # Evaluated in the calling thread at log time (before loguru enqueues),
        # so the trace-id contextvar reflects the emitting session.
        return current_trace_id() == session_id

    sink_id = _logger.add(
        str(CONFIG_ROOT / f"logs/{session_id}.txt"),
        level=level,
        format=_LOG_FORMAT,
        filter=_only_this_session,
        **_FILE_LOG_KWARGS,
    )
    _session_sink_ids[session_id] = sink_id
    return sink_id


def unbind_session_logfile(session_id: str) -> None:
    """Remove the per-session file sink for *session_id* (idempotent)."""
    sink_id = _session_sink_ids.pop(session_id, None)
    if sink_id is None:
        return
    try:
        _logger.remove(sink_id)
    except ValueError:  # already removed — nothing to do
        pass


def suspend_console_log() -> bool:
    """Remove the stderr sink so all levels go to the file only.

    Returns ``True`` if a sink was actually removed (so the caller knows whether
    a later :func:`resume_console_log` is warranted). Idempotent / best-effort.
    """
    global _console_sink_id
    if _console_sink_id is None:
        return False
    try:
        _logger.remove(_console_sink_id)
    except ValueError:  # already gone — nothing to suspend
        return False
    finally:
        _console_sink_id = None
    return True


def resume_console_log() -> None:
    """Re-add the stderr sink (at the last configured level). Idempotent."""
    global _console_sink_id
    if _console_sink_id is not None:
        return
    _console_sink_id = _logger.add(sys.stderr, level=_console_level, format=_LOG_FORMAT)


logger = define_log_level()
