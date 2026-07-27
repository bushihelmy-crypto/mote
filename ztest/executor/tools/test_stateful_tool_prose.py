"""Stateful tool prose describes capabilities without coupling tool workflows."""
from __future__ import annotations

import inspect
import re

import pytest

from mote.product.toolsets.builtin.canvas import Canvas
from mote.product.toolsets.builtin.device_use import DeviceUse
from mote.product.toolsets.builtin.python import Python
from mote.product.toolsets.builtin.terminal import Terminal
from mote.product.toolsets.builtin.web_browser import WebBrowser

_TOOLS = (Terminal, Python, WebBrowser, DeviceUse, Canvas)
_NAMES = {"Terminal", "Jupyter", "WebBrowser", "DeviceUse", "Canvas", "AskUserQuestion"}


@pytest.mark.parametrize("tool", _TOOLS, ids=lambda tool: tool.name)
def test_stateful_tool_description_does_not_prescribe_other_tools(tool):
    description = inspect.getdoc(tool.call) or ""
    forbidden = _NAMES - {tool.name}
    mentioned = {name for name in forbidden if re.search(rf"\b{re.escape(name)}\b", description)}
    assert not mentioned
