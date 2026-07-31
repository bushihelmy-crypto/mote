#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for ``mote.runtime.tools.tool_result`` (ToolResult + re-exports).

Pins the structural-failure contract: a plain return value is ALWAYS success;
failure is signalled by ``raise ToolError`` or ``ToolResult(success=False)`` —
never by sniffing the output text (so a successful output may start "Error:").
"""
from __future__ import annotations

import pytest

from mote.contracts.tool.constants import ERROR_PREFIX
from mote.runtime.errors import ToolError as ExceptionToolError
from mote.runtime.tools.tool_result import ERROR_PREFIX as RESULT_ERROR_PREFIX
from mote.runtime.tools.tool_result import FileChange, ToolError, ToolMedia, ToolResult
from mote.ztest.artifact_fakes import artifact_media, artifact_ref


class TestToolResultDefaults:
    def test_minimal_construction(self):
        r = ToolResult(output="hello")
        assert r.output == "hello"
        assert r.success is True
        assert r.data is None
        assert r.media == []
        assert r.file_changes == []

    def test_media_independent_per_instance(self):
        a = ToolResult(output="a")
        b = ToolResult(output="b")
        a.media.append(artifact_media("image", "x"))
        # default_factory => no shared mutable default between instances.
        assert b.media == []

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

    def test_image_carries_kind_ref_and_artifact(self):
        # The producer stamps ref at the source — no path recovered from data.
        r = ToolResult(
            output="Read image ...",
            media=[artifact_media("image", "image-bytes", ref="/tmp/pic.png")],
        )
        assert len(r.media) == 1
        assert r.media[0].kind == "image"
        assert r.media[0].ref == "/tmp/pic.png"
        assert r.media[0].artifact.size == len(b"image-bytes")

    def test_pdf_carries_kind_ref_and_artifact(self):
        r = ToolResult(
            output="Read PDF ...",
            media=[artifact_media("pdf", "pdf-bytes", ref="/tmp/doc.pdf")],
        )
        assert len(r.media) == 1
        assert r.media[0].kind == "pdf"
        assert r.media[0].ref == "/tmp/doc.pdf"
        assert r.media[0].mime == "application/pdf"
        assert r.media[0].artifact.size == len(b"pdf-bytes")

    def test_artifact_only_media_has_empty_ref(self):
        r = ToolResult(output="[screenshot]", media=[artifact_media("image", "shot", ref="")])
        assert len(r.media) == 1
        assert r.media[0].kind == "image"
        assert r.media[0].ref == ""

    def test_media_preserves_order_and_kind(self):
        r = ToolResult(
            output="x",
            media=[
                artifact_media("image", "a"),
                artifact_media("image", "b"),
                artifact_media("pdf", "c"),
            ],
        )
        assert [m.kind for m in r.media] == ["image", "image", "pdf"]

    def test_media_carries_typed_artifact_reference(self):
        artifact = artifact_ref(
            b"<svg/>",
            kind="canvas",
            representation="svg",
            mime_type="image/svg+xml",
        )

        media = ToolMedia(kind="image", artifact=artifact)

        assert media.artifact is artifact

    def test_media_requires_artifact_byte_source(self):
        with pytest.raises(TypeError, match="artifact"):
            ToolMedia(kind="image")  # type: ignore[call-arg]


class TestReExports:
    def test_tool_error_is_the_global_exception(self):
        # tool_result re-exports ToolError from the global exception system.
        assert ToolError is ExceptionToolError

    def test_error_prefix_reexported(self):
        assert RESULT_ERROR_PREFIX == ERROR_PREFIX
