#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
mote.runtime.logging - concrete framework logging owned by Runtime.

Provides the loguru ``logger``, the headless human-input fallback, plus
decorator logging (:func:`log_call`) and base-class decorator logging
(:class:`LoggedMixin` / :func:`log_class`) with trace-id context binding.

This package is a pure leaf: it imports only stdlib + loguru, never the higher
``common.events`` / ``common.hook`` layers. The dependency edge runs one-way
``events → logs`` (Telemetry uses ``logger``). The LLM-stream emitter
``log_llm_stream`` therefore lives in :mod:`mote.runtime.events.stream`, not
here — import it from ``mote.runtime.events``.
"""

from mote.runtime.logging.context import bind_trace, current_trace_id
from mote.runtime.logging.core import (
    bind_session_logfile,
    define_log_level,
    logger,
    resume_console_log,
    suspend_console_log,
    unbind_session_logfile,
)
from mote.runtime.logging.decorator import log_call
from mote.runtime.logging.human_input import get_human_input
from mote.runtime.logging.mixin import LoggedMixin, log_class, no_log

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
    # human input
    "get_human_input",
    # decorator logging
    "log_call",
    # base-class decorator logging
    "LoggedMixin",
    "log_class",
    "no_log",
]
