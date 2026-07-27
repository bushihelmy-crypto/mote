#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the End tool (``mote.product.toolsets.builtin.end``).

End is a thin trigger that delegates to the ``end_session`` Role capability
(faked by CapRole). Covers the delegation + that the capability's return is
passed through verbatim.
"""
from __future__ import annotations

from mote.product.toolsets.builtin.end import End

from .conftest import CapRole, bind, run


class TestEnd:
    def test_delegates_to_end_session(self, workspace):
        role = CapRole(end_output="all done — summary here")
        tool = bind(End(), role)
        out = run(tool.call())
        assert out == "all done — summary here"
        assert role.end_calls == 1

    def test_called_once_per_invocation(self, workspace):
        role = CapRole()
        tool = bind(End(), role)
        run(tool.call())
        run(tool.call())
        assert role.end_calls == 2
