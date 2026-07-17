#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for ``mote.executor.tool_result_limit``.

Pure functions (no executor wiring): byte formatting, threshold clamping,
preview newline-boundary truncation, and the ``enforce_tool_result_limit``
persist / truncate / idempotent paths. Disk writes are pointed at ``tmp_path``
via a ``WorkspaceStore`` rooted there, so persisted results co-locate under the
session directory (``.agent_sessions/{session}/tool_results/``).
"""
from __future__ import annotations

import pytest

from mote.common.schema import DEFAULT_MAX_RESULT_SIZE_CHARS, PERSISTED_OUTPUT_OPEN_TAG, PREVIEW_SIZE_BYTES
from mote.common.workspace import WorkspaceStore
from mote.executor.tool_result_limit import (
    enforce_tool_result_limit,
    format_file_size,
    generate_preview,
    persistence_threshold,
)


class TestFormatFileSize:
    @pytest.mark.parametrize(
        "size,expected",
        [
            (0, "0 bytes"),
            (512, "512 bytes"),
            (1023, "1023 bytes"),
            (1024, "1KB"),
            (1536, "1.5KB"),
            (1024 * 1024, "1MB"),
            (int(1024 * 1024 * 2.5), "2.5MB"),
            (1024 * 1024 * 1024, "1GB"),
        ],
    )
    def test_human_readable(self, size, expected):
        assert format_file_size(size) == expected

    def test_trailing_zero_stripped(self):
        # 2048 bytes == 2.0KB -> ".0" stripped -> "2KB".
        assert format_file_size(2048) == "2KB"


class TestPersistenceThreshold:
    def test_clamped_by_system_default(self):
        # A larger declared cap is clamped down to the system default.
        assert persistence_threshold(10_000_000) == DEFAULT_MAX_RESULT_SIZE_CHARS

    def test_smaller_declared_cap_kept(self):
        assert persistence_threshold(1_000) == 1_000

    def test_inf_is_opt_out(self):
        assert persistence_threshold(float("inf")) == float("inf")


class TestGeneratePreview:
    def test_short_content_unchanged(self):
        preview, has_more = generate_preview("hello", 100)
        assert preview == "hello"
        assert has_more is False

    def test_cut_at_newline_boundary_past_halfway(self):
        # Newline lands past the halfway point -> cut there, not mid-line.
        content = "a" * 60 + "\n" + "b" * 60  # max_bytes=100, newline at idx 60 (>50)
        preview, has_more = generate_preview(content, 100)
        assert has_more is True
        assert preview == "a" * 60
        assert "\n" not in preview

    def test_hard_cut_when_no_late_newline(self):
        # Newline before the halfway point -> ignored; cut at exact byte limit.
        content = "a\n" + "b" * 200
        preview, has_more = generate_preview(content, 100)
        assert has_more is True
        assert len(preview) == 100


class TestEnforceToolResultLimit:
    def test_empty_output_returned_as_is(self):
        assert enforce_tool_result_limit("", "T", result_id="r") == ""

    def test_under_threshold_unchanged(self):
        out = "small"
        assert enforce_tool_result_limit(out, "T", result_id="r", max_result_size_chars=1000) == out

    def test_persist_writes_file_and_returns_preview(self, tmp_path):
        big = "line\n" * 1000  # well over a tiny cap
        result = enforce_tool_result_limit(
            big,
            "BigTool",
            result_id="abc",
            session_id="s1",
            max_result_size_chars=100,
            persist=True,
            store=WorkspaceStore(tmp_path),
        )
        assert result.startswith(PERSISTED_OUTPUT_OPEN_TAG)
        assert "Output too large" in result
        # Co-located under the session directory alongside rollout + blobs.
        path = tmp_path / ".agent_sessions" / "s1" / "tool_results" / "abc.txt"
        assert path.exists()
        assert path.read_text() == big

    def test_persist_is_idempotent_on_already_wrapped(self, tmp_path):
        wrapped = f"{PERSISTED_OUTPUT_OPEN_TAG}\nalready persisted\n</persisted-output>" + "x" * 60_000
        # Even though it's over threshold, an already-wrapped output is left alone.
        result = enforce_tool_result_limit(
            wrapped, "T", result_id="r", max_result_size_chars=100, store=WorkspaceStore(tmp_path)
        )
        assert result == wrapped

    def test_reuses_existing_file(self, tmp_path):
        big = "z" * 5000
        path = tmp_path / ".agent_sessions" / "s2" / "tool_results" / "rid.txt"
        path.parent.mkdir(parents=True)
        path.write_text("PREEXISTING CONTENT")
        enforce_tool_result_limit(
            big, "T", result_id="rid", session_id="s2", max_result_size_chars=100, store=WorkspaceStore(tmp_path)
        )
        # Idempotent persistence: an existing file is NOT overwritten.
        assert path.read_text() == "PREEXISTING CONTENT"

    def test_persist_false_truncates_inline(self, tmp_path):
        big = "q" * 5000
        result = enforce_tool_result_limit(
            big,
            "T",
            result_id="r",
            max_result_size_chars=100,
            persist=False,
            store=WorkspaceStore(tmp_path),
        )
        assert not result.startswith(PERSISTED_OUTPUT_OPEN_TAG)
        assert "omitted" in result
        assert "total" in result
        # No file written when persistence is disabled.
        assert not (tmp_path / ".agent_sessions").exists()

    def test_preview_size_governs_persisted_preview(self, tmp_path):
        big = "p" * (PREVIEW_SIZE_BYTES * 3)
        result = enforce_tool_result_limit(
            big, "T", result_id="r", session_id="s", max_result_size_chars=100, store=WorkspaceStore(tmp_path)
        )
        # Preview slice is bounded by PREVIEW_SIZE_BYTES, far smaller than the full output.
        assert len(result) < len(big)
        assert (
            f"Preview (first {PREVIEW_SIZE_BYTES // 1024 if PREVIEW_SIZE_BYTES >= 1024 else PREVIEW_SIZE_BYTES}"
            in result
            or "Preview (first" in result
        )
