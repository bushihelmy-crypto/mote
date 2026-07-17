#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Tool output logging.

:class:`ToolLogItem` is the unit of tool output. ``log_tool_output`` /
``log_tool_output_async`` forward to pluggable sinks (no-op by default) so the
output can be routed to different destinations via the setters. Sink, setter and
caller share this module so the ``global`` rebind works.
"""

from __future__ import annotations

from typing import Any, Union

from pydantic import BaseModel, Field


class ToolLogItem(BaseModel):
    type_: str = Field(alias="type", default="str", description="Data type of `value` field.")
    name: str
    value: Any


# A special log item that marks the end of a stream log.
TOOL_LOG_END_MARKER = ToolLogItem(type="str", name="end_marker", value="\x18\x19\x1B\x18")


def _tool_output_log(*args, **kwargs):
    """Default no-op sink, replaced via :func:`set_tool_output_logfunc`."""
    return None


async def _tool_output_log_async(*args, **kwargs):
    """Default async no-op sink, replaced via :func:`set_tool_output_logfunc_async`."""
    return None


def set_tool_output_logfunc(func):
    """Replace the synchronous tool-output sink."""
    global _tool_output_log
    _tool_output_log = func


async def set_tool_output_logfunc_async(func):
    """Replace the asynchronous tool-output sink."""
    global _tool_output_log_async
    _tool_output_log_async = func


def log_tool_output(output: Union[ToolLogItem, list[ToolLogItem]], tool_name: str = ""):
    """Log tool output through the current synchronous sink."""
    _tool_output_log(output=output, tool_name=tool_name)


async def log_tool_output_async(output: Union[ToolLogItem, list[ToolLogItem]], tool_name: str = ""):
    """Log tool output through the current asynchronous sink (for async objects)."""
    await _tool_output_log_async(output=output, tool_name=tool_name)
