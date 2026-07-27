#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for mote.router.llm.editor_read_parser (Editor.read segment parsing)."""
from __future__ import annotations

from mote.runtime.models.clients.editor_read_parser import EDITOR_READ_MARKER, find_editor_read_segments


class TestFindEditorReadSegments:
    def test_no_marker_returns_empty(self):
        assert find_editor_read_segments("nothing relevant here") == []

    def test_single_new_shape_path_content(self):
        text = f"{EDITOR_READ_MARKER} path=/tmp/a.py content=print('hi')"
        segs = find_editor_read_segments(text)
        assert len(segs) == 1
        assert segs[0]["file_path"] == "/tmp/a.py"
        assert segs[0]["block_content"] == "print('hi')"
        assert segs[0]["start"] == 0
        assert segs[0]["end"] == len(text)

    def test_single_old_shape_file_path_block_content(self):
        text = f"{EDITOR_READ_MARKER} file_path=/tmp/b.py block_content=line1\nline2"
        segs = find_editor_read_segments(text)
        assert len(segs) == 1
        assert segs[0]["file_path"] == "/tmp/b.py"
        assert segs[0]["block_content"] == "line1\nline2"

    def test_path_with_spaces_absorbed(self):
        text = f"{EDITOR_READ_MARKER} path='/My Drive/x.py' content=body"
        segs = find_editor_read_segments(text)
        assert len(segs) == 1
        assert segs[0]["file_path"] == "/My Drive/x.py"
        assert segs[0]["block_content"] == "body"

    def test_segment_bounded_by_next_command(self):
        text = f"{EDITOR_READ_MARKER} path=/a.py content=AAA" "\n\nCommand Bash executed: ls"
        segs = find_editor_read_segments(text)
        assert len(segs) == 1
        assert segs[0]["block_content"] == "AAA"
        # end stops at the boundary, not the whole string
        assert segs[0]["end"] < len(text)

    def test_multiple_segments(self):
        text = f"{EDITOR_READ_MARKER} path=/a.py content=AAA" "\n\n" f"{EDITOR_READ_MARKER} path=/b.py content=BBB"
        segs = find_editor_read_segments(text)
        assert len(segs) == 2
        assert [s["file_path"] for s in segs] == ["/a.py", "/b.py"]
        assert [s["block_content"] for s in segs] == ["AAA", "BBB"]

    def test_quoted_content_outer_quotes_stripped(self):
        text = f'{EDITOR_READ_MARKER} path=/a.py content="hello world"'
        segs = find_editor_read_segments(text)
        assert segs[0]["block_content"] == "hello world"
