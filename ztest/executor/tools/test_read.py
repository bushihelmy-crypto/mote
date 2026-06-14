#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the Read tool (``metagpt.executor.tools.read``).

Covers text slicing (offset/limit + line numbers), the empty/short-file and
binary/device guards, notebook flattening, image media results, the per-instance
dedup cache, and the shared file-read-state recording used by Write/Edit.
"""
from __future__ import annotations

import base64
import json
import os

import pytest

from metagpt.executor.tool_result import ToolError, ToolResult
from metagpt.executor.tools.read import Read, FILE_UNCHANGED_STUB
from metagpt.common.const.tools import MAX_LINE_LENGTH

from .conftest import CapRole, bind, run, write_file


def _read(tool: Read, **kwargs):
    return run(tool.call(**kwargs))


class TestReadText:
    def test_reads_with_cat_n_line_numbers(self, workspace):
        p = write_file(workspace / "a.txt", "alpha\nbeta\ngamma\n")
        out = _read(Read(), file_path=p)
        # Right-aligned line numbers, arrow separator, 1-indexed.
        assert "     1→alpha" in out
        assert "     2→beta" in out
        assert "     3→gamma" in out

    def test_offset_and_limit_slice(self, workspace):
        p = write_file(workspace / "a.txt", "\n".join(f"line{i}" for i in range(1, 11)) + "\n")
        out = _read(Read(), file_path=p, offset=3, limit=2)
        assert "     3→line3" in out
        assert "     4→line4" in out
        assert "line2" not in out
        assert "line5" not in out

    def test_relative_path_resolves_against_cwd(self, workspace):
        write_file(workspace / "rel.txt", "hi\n")
        out = _read(Read(), file_path="rel.txt")
        assert "1→hi" in out

    def test_empty_file_warns(self, workspace):
        p = write_file(workspace / "empty.txt", "")
        out = _read(Read(), file_path=p)
        assert "contents are empty" in out

    def test_offset_past_end_warns(self, workspace):
        p = write_file(workspace / "a.txt", "one\ntwo\n")
        out = _read(Read(), file_path=p, offset=99)
        assert "shorter than the provided offset" in out
        assert "2 lines" in out

    def test_long_line_truncated_with_note(self, workspace):
        p = write_file(workspace / "long.txt", "x" * (MAX_LINE_LENGTH + 50) + "\n")
        out = _read(Read(), file_path=p)
        assert "[line truncated]" in out
        assert "were truncated" in out

    def test_missing_file_raises(self, workspace):
        with pytest.raises(ToolError, match="does not exist"):
            _read(Read(), file_path=str(workspace / "nope.txt"))

    def test_directory_raises(self, workspace):
        with pytest.raises(ToolError, match="is a directory"):
            _read(Read(), file_path=str(workspace))

    def test_empty_path_raises(self, workspace):
        with pytest.raises(ToolError, match="'file_path' argument is required"):
            _read(Read(), file_path="   ")

    def test_invalid_mode_raises(self, workspace):
        p = write_file(workspace / "a.txt", "hi\n")
        with pytest.raises(ToolError, match="invalid mode"):
            _read(Read(), file_path=p, mode="sideways")


class TestReadGuards:
    def test_binary_extension_refused(self, workspace):
        p = write_file(workspace / "blob.zip", "not really a zip")
        with pytest.raises(ToolError, match="cannot read binary"):
            _read(Read(), file_path=p)

    def test_non_utf8_text_refused(self, workspace):
        p = os.path.join(str(workspace), "bin.txt")
        with open(p, "wb") as f:
            f.write(b"\xff\xfe\x00\x01rubbish")
        with pytest.raises(ToolError, match="not valid UTF-8"):
            _read(Read(), file_path=p)

    def test_blocked_device_path_refused(self):
        with pytest.raises(ToolError, match="block or produce infinite output"):
            _read(Read(), file_path="/dev/zero")


class TestReadDedup:
    def test_same_range_unchanged_returns_stub(self, workspace):
        p = write_file(workspace / "a.txt", "one\ntwo\n")
        tool = Read()
        first = _read(tool, file_path=p)
        assert "1→one" in first
        # Same instance + same range + unchanged mtime => dedup stub.
        second = _read(tool, file_path=p)
        assert second == FILE_UNCHANGED_STUB

    def test_changed_file_rereads(self, workspace):
        p = write_file(workspace / "a.txt", "one\n")
        tool = Read()
        _read(tool, file_path=p)
        # Rewrite with new content and force a distinct mtime (writes within the
        # same clock tick can otherwise leave mtime_ns unchanged).
        write_file(workspace / "a.txt", "one\ntwo\n")
        st = os.stat(p)
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        out = _read(tool, file_path=p)
        assert out != FILE_UNCHANGED_STUB
        assert "2→two" in out

    def test_cleanup_session_clears_cache(self, workspace):
        p = write_file(workspace / "a.txt", "one\n")
        tool = Read()
        _read(tool, file_path=p)
        tool.cleanup_session("sess")
        # Cache cleared => not a dedup stub on the next read.
        assert _read(tool, file_path=p) != FILE_UNCHANGED_STUB


class TestReadRecordsState:
    def test_read_records_into_shared_state(self, workspace):
        p = write_file(workspace / "a.txt", "one\n")
        role = CapRole()
        tool = bind(Read(), role)
        _read(tool, file_path=p)
        assert role.get_file_read_mtime(p) == os.stat(p).st_mtime_ns

    def test_unbound_read_skips_recording(self, workspace):
        p = write_file(workspace / "a.txt", "one\n")
        tool = Read()  # not bound — no record_file_read injected
        # Must not raise; just returns content.
        assert "1→one" in _read(tool, file_path=p)


class TestReadNotebook:
    def test_flattens_cells_and_outputs(self, workspace):
        nb = {
            "cells": [
                {"cell_type": "markdown", "source": ["# Title\n"]},
                {
                    "cell_type": "code",
                    "source": "print('hi')\n",
                    "outputs": [{"output_type": "stream", "text": "hi\n"}],
                },
            ],
            "nbformat": 4,
            "nbformat_minor": 5,
        }
        p = write_file(workspace / "nb.ipynb", json.dumps(nb))
        out = _read(Read(), file_path=p)
        assert "Cell 1 [markdown]" in out
        assert "# Title" in out
        assert "Cell 2 [code]" in out
        assert "print('hi')" in out
        assert "# Output:" in out
        assert "hi" in out

    def test_invalid_notebook_json_raises(self, workspace):
        p = write_file(workspace / "bad.ipynb", "{not json")
        with pytest.raises(ToolError, match="not a valid notebook"):
            _read(Read(), file_path=p)


class TestReadImage:
    def test_image_returns_media_toolresult(self, workspace):
        # 1x1 transparent PNG.
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        )
        p = os.path.join(str(workspace), "img.png")
        with open(p, "wb") as f:
            f.write(png)
        result = _read(Read(), file_path=p)
        assert isinstance(result, ToolResult)
        assert result.images and isinstance(result.images[0], str)
        # The embedded base64 round-trips to the original bytes.
        assert base64.b64decode(result.images[0]) == png
        assert result.data["type"] == "image"
