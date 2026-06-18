#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for the unified error-presentation contract.

``ErrorReport.from_exception`` normalizes any exception (typed or un-typed) into
one record, and ``render_error_block`` is the single renderer producing the
``<error …>`` block every executor surface (tool / graph) shows the LLM.
"""
from __future__ import annotations

import pytest

from metagpt.common.exception import (
    ErrorReport,
    GraphBatchFailureError,
    GraphNodeRetryExhaustedError,
    GraphParamTypeError,
    GraphRecursionError,
    ToolError,
    render_error_block,
)
from metagpt.common.exception.codes import ErrorCode, RecoveryAction


class TestFromException:
    def test_typed_error_carries_full_contract(self):
        report = ErrorReport.from_exception(ToolError("missing file"))
        assert report.error == "ToolError"
        assert report.code == ErrorCode.TOOL.value
        assert report.message == "missing file"
        assert report.retryable is False
        assert report.recovery == RecoveryAction.ABORT.value
        assert report.cause is None

    def test_untyped_exception_degrades_to_unknown(self):
        report = ErrorReport.from_exception(RuntimeError("kaboom"))
        assert report.error == "RuntimeError"
        assert report.code == ErrorCode.UNKNOWN.value
        assert report.message == "kaboom"
        # Retry classification reuses handlers.is_retryable (a RuntimeError is not).
        assert report.retryable is False
        assert report.recovery == RecoveryAction.ABORT.value
        assert report.detail == {"type": "RuntimeError"}

    def test_empty_message_falls_back_to_class_name(self):
        report = ErrorReport.from_exception(RuntimeError())
        assert report.message == "RuntimeError"

    def test_cause_is_captured_as_repr(self):
        cause = ValueError("root")
        report = ErrorReport.from_exception(
            GraphNodeRetryExhaustedError("tts", 3, cause)
        )
        assert report.code == ErrorCode.GRAPH_NODE_RETRY_EXHAUSTED.value
        assert report.detail == {"node": "tts", "attempts": 3}
        assert "root" in report.cause


class TestSerialization:
    def test_as_dict_from_dict_round_trip(self):
        report = ErrorReport.from_exception(GraphRecursionError("budget blown"))
        rebuilt = ErrorReport.from_dict(report.as_dict())
        assert rebuilt == report

    def test_from_dict_tolerates_missing_keys(self):
        report = ErrorReport.from_dict({"message": "partial"})
        assert report.message == "partial"
        assert report.code == ErrorCode.UNKNOWN.value
        assert report.recovery == RecoveryAction.ABORT.value
        assert report.detail == {}


class TestRenderErrorBlock:
    def test_block_carries_machine_readable_attrs(self):
        block = render_error_block(ErrorReport.from_exception(ToolError("nope")))
        assert block.startswith('<error code="TOOL" recovery="abort" retryable="false">')
        assert "nope" in block
        assert block.endswith("</error>")

    def test_detail_keys_rendered_indented(self):
        block = render_error_block(
            ErrorReport.from_exception(
                GraphParamTypeError("tts", "text", str, int)
            )
        )
        assert "  node: tts" in block
        assert "  param: text" in block
        assert "  expected: str" in block
        assert "  got: int" in block

    def test_type_key_is_not_rendered(self):
        # The redundant {"type": ...} marker on un-typed errors is suppressed.
        block = render_error_block(ErrorReport.from_exception(RuntimeError("x")))
        assert "type:" not in block

    def test_cause_line_rendered(self):
        block = render_error_block(
            ErrorReport.from_exception(
                GraphNodeRetryExhaustedError("tts", 2, ValueError("root"))
            )
        )
        assert "cause: " in block
        assert "root" in block


class TestGraphBatchFailureDetail:
    def test_failures_expand_to_per_node_lines(self):
        err = GraphBatchFailureError(
            [("tts", ToolError("bad audio")), ("img", RuntimeError("oom"))]
        )
        report = ErrorReport.from_exception(err)
        failures = report.detail["failures"]
        assert [f["node"] for f in failures] == ["tts", "img"]
        # Each nested failure is itself a normalized report.
        assert failures[0]["code"] == ErrorCode.TOOL.value
        assert failures[1]["code"] == ErrorCode.UNKNOWN.value

        block = render_error_block(report)
        assert "failed nodes:" in block
        assert "- tts [TOOL]: bad audio" in block
        # UNKNOWN code is not noisy-tagged in the per-node line.
        assert "- img: oom" in block
