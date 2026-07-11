#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for ``mote.executor.tool_result`` (ToolResult + re-exports).

Pins the structural-failure contract: a plain return value is ALWAYS success;
failure is signalled by ``raise ToolError`` or ``ToolResult(success=False)`` —
never by sniffing the output text (so a successful output may start "Error:").
"""
from __future__ import annotations

from mote.common.const.tools import ERROR_PREFIX
from mote.common.exception import ToolError as ExceptionToolError
from mote.executor.tool_result import ERROR_PREFIX as RESULT_ERROR_PREFIX
from mote.executor.tool_result import FileChange, ToolError, ToolMedia, ToolResult


class TestToolResultDefaults:
    def test_minimal_construction(self):
        r = ToolResult(output="hello")
        assert r.output == "hello"
        assert r.success is True
        assert r.data is None
        assert r.images == []
        assert r.pdfs == []
        assert r.file_changes == []

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


class TestFileChange:
    def test_defaults_are_empty(self):
        # A bare FileChange is all-empty; the view layer degrades gracefully.
        c = FileChange()
        assert (c.path, c.old, c.new) == ("", "", "")

    def test_update_carries_both_sides(self):
        c = FileChange(path="/tmp/a.py", old="x = 1\n", new="x = 2\n")
        assert c.old == "x = 1\n"
        assert c.new == "x = 2\n"

    def test_creation_has_empty_old(self):
        c = FileChange(path="/tmp/new.py", old="", new="hello\n")
        assert c.old == ""
        assert c.new == "hello\n"

    def test_deletion_has_empty_new(self):
        c = FileChange(path="/tmp/gone.py", old="bye\n", new="")
        assert c.old == "bye\n"
        assert c.new == ""

    def test_file_changes_independent_per_instance(self):
        a = ToolResult(output="a")
        b = ToolResult(output="b")
        a.file_changes.append(FileChange(path="/tmp/a.py"))
        # default_factory => no shared mutable default between instances.
        assert b.file_changes == []


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


class TestMediaArtifacts:
    def test_no_media_is_empty(self):
        assert ToolResult(output="x").media_artifacts() == []

    def test_image_with_path_recovers_ref(self):
        r = ToolResult(
            output="Read image ...",
            images=["<b64>"],
            data={"type": "image", "path": "/tmp/pic.png"},
        )
        media = r.media_artifacts()
        assert len(media) == 1
        assert media[0].kind == "image"
        assert media[0].ref == "/tmp/pic.png"

    def test_pdf_with_path_recovers_ref(self):
        r = ToolResult(
            output="Read PDF ...",
            pdfs=["<b64>"],
            data={"type": "pdf", "path": "/tmp/doc.pdf"},
        )
        media = r.media_artifacts()
        assert len(media) == 1
        assert media[0].kind == "pdf"
        assert media[0].ref == "/tmp/doc.pdf"

    def test_bytes_only_media_has_empty_ref(self):
        # A screenshot carries base64 but no on-disk path (data has no "path").
        r = ToolResult(output="[screenshot]", images=["<b64>"], data={"type": "screenshot"})
        media = r.media_artifacts()
        assert len(media) == 1
        assert media[0].kind == "image"
        assert media[0].ref == ""

    def test_multiple_artifacts(self):
        r = ToolResult(output="x", images=["<a>", "<b>"], pdfs=["<c>"])
        media = r.media_artifacts()
        assert [m.kind for m in media] == ["image", "image", "pdf"]


class TestReExports:
    def test_tool_error_is_the_global_exception(self):
        # tool_result re-exports ToolError from the global exception system.
        assert ToolError is ExceptionToolError

    def test_error_prefix_reexported(self):
        assert RESULT_ERROR_PREFIX == ERROR_PREFIX
