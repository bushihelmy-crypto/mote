#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
mote.common.logs — the framework logging package (replaces the old logs.py).

Provides the loguru ``logger``, tool-output / human-input helpers, plus
decorator logging (:func:`log_call`) and base-class decorator logging
(:class:`LoggedMixin` / :func:`log_class`) with trace-id context binding.

This package is a pure leaf: it imports only stdlib + loguru, never the higher
``common.events`` / ``common.hook`` layers. The dependency edge runs one-way
``events → logs`` (the bus uses ``logger``). The LLM-stream emitter
``log_llm_stream`` therefore lives in :mod:`mote.common.events.stream`, not
here — import it from ``mote.common.events``.
"""

from mote.common.logs.context import bind_trace, current_trace_id
from mote.common.logs.core import (
    bind_session_logfile,
    define_log_level,
    logger,
    resume_console_log,
    suspend_console_log,
    unbind_session_logfile,
)
from mote.common.logs.decorator import log_call
from mote.common.logs.human_input import get_human_input, set_human_input_func
from mote.common.logs.mixin import LoggedMixin, log_class, no_log
from mote.common.logs.tool_output import (
    TOOL_LOG_END_MARKER,
    ToolLogItem,
    log_tool_output,
    log_tool_output_async,
    set_tool_output_logfunc,
    set_tool_output_logfunc_async,
)

__all__ = [
    # core
    "logger",
    "define_log_level",
    "suspend_console_log",
    "resume_console_log",
    "bind_session_logfile",
    "unbind_session_logfile",
    # trace context
    "bind_trace",
    "current_trace_id",
    # tool output
    "ToolLogItem",
    "TOOL_LOG_END_MARKER",
    "log_tool_output",
    "log_tool_output_async",
    "set_tool_output_logfunc",
    "set_tool_output_logfunc_async",
    # human input
    "get_human_input",
    "set_human_input_func",
    # decorator logging
    "log_call",
    # base-class decorator logging
    "LoggedMixin",
    "log_class",
    "no_log",
]
