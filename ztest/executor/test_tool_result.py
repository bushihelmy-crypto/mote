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

    def test_media_independent_per_instance(self):
        a = ToolResult(output="a")
        b = ToolResult(output="b")
        a.media.append(ToolMedia(kind="image", b64="x"))
        # default_factory => no shared mutable default between instances.
        assert b.media == []
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


class TestMedia:
    def test_no_media_is_empty(self):
        r = ToolResult(output="x")
        assert r.media == []
        assert r.images == []
        assert r.pdfs == []

    def test_image_carries_kind_ref_b64(self):
        # The producer stamps ref at the source — no path recovered from data.
        r = ToolResult(
            output="Read image ...",
            media=[ToolMedia(kind="image", b64="<b64>", ref="/tmp/pic.png")],
        )
        assert len(r.media) == 1
        assert r.media[0].kind == "image"
        assert r.media[0].ref == "/tmp/pic.png"
        assert r.media[0].b64 == "<b64>"
        # .images projects the base64 payloads grouped by kind.
        assert r.images == ["<b64>"]
        assert r.pdfs == []

    def test_pdf_carries_kind_ref_b64(self):
        r = ToolResult(
            output="Read PDF ...",
            media=[ToolMedia(kind="pdf", b64="<b64>", ref="/tmp/doc.pdf", mime="application/pdf")],
        )
        assert len(r.media) == 1
        assert r.media[0].kind == "pdf"
        assert r.media[0].ref == "/tmp/doc.pdf"
        assert r.media[0].mime == "application/pdf"
        assert r.pdfs == ["<b64>"]
        assert r.images == []

    def test_bytes_only_media_has_empty_ref(self):
        # A screenshot carries base64 but no on-disk path.
        r = ToolResult(output="[screenshot]", media=[ToolMedia(kind="image", b64="<b64>", ref="")])
        assert len(r.media) == 1
        assert r.media[0].kind == "image"
        assert r.media[0].ref == ""
        assert r.images == ["<b64>"]

    def test_images_pdfs_project_by_kind(self):
        r = ToolResult(
            output="x",
            media=[
                ToolMedia(kind="image", b64="<a>"),
                ToolMedia(kind="image", b64="<b>"),
                ToolMedia(kind="pdf", b64="<c>"),
            ],
        )
        assert [m.kind for m in r.media] == ["image", "image", "pdf"]
        assert r.images == ["<a>", "<b>"]
        assert r.pdfs == ["<c>"]


class TestReExports:
    def test_tool_error_is_the_global_exception(self):
        # tool_result re-exports ToolError from the global exception system.
        assert ToolError is ExceptionToolError

    def test_error_prefix_reexported(self):
        assert RESULT_ERROR_PREFIX == ERROR_PREFIX
