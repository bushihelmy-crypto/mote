#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for ``metagpt.executor.tool_result`` (ToolResult + re-exports).

Pins the structural-failure contract: a plain return value is ALWAYS success;
failure is signalled by ``raise ToolError`` or ``ToolResult(success=False)`` —
never by sniffing the output text (so a successful output may start "Error:").
"""
from __future__ import annotations

from metagpt.common.const.tools import ERROR_PREFIX
from metagpt.common.exception import ToolError as ExceptionToolError
from metagpt.executor.tool_result import ERROR_PREFIX as RESULT_ERROR_PREFIX
from metagpt.executor.tool_result import ToolError, ToolResult


class TestToolResultDefaults:
    def test_minimal_construction(self):
        r = ToolResult(output="hello")
        assert r.output == "hello"
        assert r.success is True
        assert r.data is None
        assert r.images == []
        assert r.pdfs == []

    def test_images_pdfs_independent_per_instance(self):
        a = ToolResult(output="a")
        b = ToolResult(output="b")
        a.images.append("x")
        a.pdfs.append("y")
        # default_factory => no shared mutable default between instances.
        assert b.images == []
        assert b.pdfs == []

    def test_explicit_failure(self):
        r = ToolResult(output="nope", success=False, data={"k": 1})
        assert r.success is False
        assert r.data == {"k": 1}


class TestFromToolReturn:
    def test_passthrough_existing_toolresult(self):
        original = ToolResult(output="x", success=False)
        assert ToolResult.from_tool_return(original) is original

    def test_plain_string_is_success(self):
        r = ToolResult.from_tool_return("done")
        assert r.output == "done"
        assert r.success is True

    def test_none_becomes_empty_string(self):
        r = ToolResult.from_tool_return(None)
        assert r.output == ""
        assert r.success is True

    def test_non_string_value_is_stringified(self):
        r = ToolResult.from_tool_return(42)
        assert r.output == "42"
        assert r.success is True

    def test_error_prefixed_text_still_success(self):
        # The "Error:" prefix is a text convention only — it does NOT drive success.
        r = ToolResult.from_tool_return(f"{ERROR_PREFIX} matched line")
        assert r.success is True
        assert r.output.startswith(ERROR_PREFIX)


class TestReExports:
    def test_tool_error_is_the_global_exception(self):
        # tool_result re-exports ToolError from the global exception system.
        assert ToolError is ExceptionToolError

    def test_error_prefix_reexported(self):
        assert RESULT_ERROR_PREFIX == ERROR_PREFIX
