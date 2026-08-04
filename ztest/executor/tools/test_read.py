#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the Read tool (``mote.product.toolsets.builtin.read``).

Covers text slicing (offset/limit + line numbers), the empty/short-file and
binary/device guards, notebook flattening, image media results, the per-instance
dedup cache, and the shared file-read-state recording used by Write/Edit.
"""

from __future__ import annotations

import base64
import io
import json
import os

import pytest

from mote.contracts.tool.errors import ToolError
from mote.product.toolsets.builtin.read import FILE_UNCHANGED_STUB, Read
from mote.runtime.tools.tool_result import ToolResult

from .conftest import CapRole, bind, run, write_file


def _read(tool: Read, **kwargs) -> str:
    """Drive a read and return its text output.

    Read now returns real content as a ``ToolResult`` (tagged with the source
    ``resource_path``) and the dedup stub as a bare string; the executor
    normalizes both via :meth:`ToolResult.from_tool_return`. We mirror that here
    and expose ``.output`` so text assertions stay simple.
    """
    if not hasattr(tool, "capture_file_snapshot"):
        tool = bind(tool, CapRole())
    return ToolResult.from_tool_return(run(tool.call(**kwargs))).output


def _read_result(tool: Read, **kwargs) -> ToolResult:
    """Like ``_read`` but return the full normalized ToolResult (media/metadata)."""
    if not hasattr(tool, "capture_file_snapshot"):
        tool = bind(tool, CapRole())
    return ToolResult.from_tool_return(run(tool.call(**kwargs)))


def _artifact_bytes(role: CapRole, result: ToolResult, index: int = 0) -> bytes:
    artifact = result.media[index].artifact
    assert artifact is not None
    return run(role.artifact_store.read(artifact))


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

    def test_partial_text_result_continues_from_immutable_cursor(self, workspace):
        p = write_file(workspace / "paged.txt", "one\ntwo\nthree")
        tool = Read()
        role = CapRole()
        first = _read_result(bind(tool, role), file_path=p, limit=1)
        os.unlink(p)
        second = _read_result(
            tool,
            file_path=p,
            limit=1,
            cursor=first.data["next_cursor"],
        )

        assert first.data["status"] == "partial"
        assert first.data["next_cursor"]
        assert "1→one" in first.output
        assert "2→two" in second.output
        assert "changed" not in second.output
        assert second.data["snapshot_digest"] == first.data["snapshot_digest"]

    def test_relative_path_resolves_against_cwd(self, workspace):
        write_file(workspace / "rel.txt", "hi\n")
        out = _read(Read(), file_path="rel.txt")
        assert "1→hi" in out

    def test_relative_path_resolves_against_role_cwd(self, workspace, tmp_path):
        # A bound tool resolves relative paths against the ROLE's stable cwd, not
        # the process cwd (which the workspace fixture chdir'd into).
        sub = tmp_path / "role_dir"
        sub.mkdir()
        write_file(sub / "here.txt", "role-side\n")
        # A same-named file in the process cwd must NOT be the one read.
        write_file(workspace / "here.txt", "process-side\n")
        role = CapRole(cwd=str(sub))
        out = _read(bind(Read(), role), file_path="here.txt")
        assert "role-side" in out
        assert "process-side" not in out

    def test_relative_path_unbound_uses_process_cwd(self, workspace):
        # Unbound (no Role): relative paths fall back to the process cwd.
        write_file(workspace / "unbound.txt", "ok\n")
        out = _read(Read(), file_path="unbound.txt")
        assert "1→ok" in out

    def test_empty_file_warns(self, workspace):
        p = write_file(workspace / "empty.txt", "")
        out = _read(Read(), file_path=p)
        assert "contents are empty" in out

    def test_offset_past_end_warns(self, workspace):
        p = write_file(workspace / "a.txt", "one\ntwo\n")
        out = _read(Read(), file_path=p, offset=99)
        assert "shorter than the provided offset" in out
        assert "3 lines" in out

    def test_long_line_returned_intact(self, workspace):
        # Per-line truncation was removed; a large result is handled by the
        # shared persist-to-disk exit, not truncated here.
        long_line = "x" * 3000
        p = write_file(workspace / "long.txt", long_line + "\n")
        out = _read(Read(), file_path=p)
        assert long_line in out
        assert "[line truncated]" not in out

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
        with pytest.raises(ToolError, match="lossless text encoding"):
            _read(Read(), file_path=p)

    def test_blocked_device_path_refused(self):
        with pytest.raises(ToolError, match="block or produce infinite output"):
            _read(Read(), file_path="/dev/zero")

    def test_video_missing_file_raises(self, workspace):
        # A video path is recognised before the binary rejection, but a missing
        # file still fails with the standard "does not exist" error (not a blunt
        # "cannot read binary").
        with pytest.raises(ToolError, match="does not exist"):
            _read(Read(), file_path=str(workspace / "nope.mp4"))


class TestReadPermissionTarget:
    def test_relative_path_is_canonicalized_against_role_cwd(
        self,
        workspace,
        tmp_path,
    ):
        role_dir = tmp_path / "role_dir"
        role_dir.mkdir()
        tool = bind(Read(), CapRole(cwd=str(role_dir)))

        target = tool.permission_target({"file_path": "nested/../target.txt"})

        assert target == os.path.realpath(role_dir / "target.txt")
        assert target != os.path.realpath(workspace / "target.txt")

    def test_symlink_target_resolves_to_its_canonical_path(self, workspace):
        target = workspace / "target.txt"
        target.write_text("content", encoding="utf-8")
        alias = workspace / "alias.txt"
        alias.symlink_to(target)
        tool = bind(Read(), CapRole(cwd=str(workspace)))

        assert tool.permission_target({"file_path": "alias.txt"}) == os.path.realpath(target)


class TestReadByteViews:
    def test_raw_reads_binary_as_reversible_base64_from_byte_zero(self, workspace):
        path = workspace / "payload.zip"
        path.write_bytes(b"\x00\xffABC")

        result = _read_result(Read(), file_path=str(path), mode="raw", limit=3)

        assert "base64:AP9B" in result.output
        assert result.data["byte_offset"] == 0
        assert result.data["next_offset"] == 3
        assert result.data["encoding"] == "base64"

    def test_hex_reads_arbitrary_byte_offset(self, workspace):
        path = workspace / "payload.bin"
        path.write_bytes(b"012ABC\xff")

        result = _read_result(
            Read(),
            file_path=str(path),
            mode="hex",
            offset=3,
            limit=4,
        )

        assert "0000000000000003  41 42 43 ff" in result.output
        assert "|ABC.|" in result.output
        assert result.data["status"] == "complete"

    def test_invalid_byte_range_is_a_tool_error(self, workspace):
        path = workspace / "payload.bin"
        path.write_bytes(b"data")

        with pytest.raises(ToolError, match="byte offset must be non-negative"):
            _read(Read(), file_path=str(path), mode="raw", offset=-1)


class TestReadVideo:
    """Read absorbs a LOCAL video file: frames as image media + a transcript.

    The heavy ffmpeg/ffprobe decode lives in the shared ``_video`` kernel and is
    external-process work, so these tests drive Read's own branch with
    ``decompose_video`` monkeypatched — fully offline.
    """

    def _fake_result(self, **kw):
        from mote.runtime.media.video import VideoFrame, VideoResult

        frames = kw.pop(
            "frames",
            [VideoFrame(timestamp=0.0, jpeg=b"\xff\xd8jpg", reason="first-frame")],
        )
        return VideoResult(
            frames=frames,
            transcript=kw.pop("transcript", ""),
            meta=kw.pop("meta", {"title": "clip.mp4", "duration_seconds": 12}),
            engine=kw.pop("engine", "keyframe"),
            notes=kw.pop("notes", []),
        )

    def test_frames_become_image_media(self, workspace, monkeypatch):
        import mote.product.toolsets.builtin.read_adapters.video as video_adapter
        from mote.runtime.media.video import VideoFrame

        frames = [
            VideoFrame(timestamp=0.0, jpeg=b"\xff\xd8a", reason="first-frame"),
            VideoFrame(timestamp=5.0, jpeg=b"\xff\xd8b", reason="keyframe"),
        ]

        async def fake(*a, **k):
            return self._fake_result(frames=frames)

        monkeypatch.setattr(video_adapter, "decompose_video", fake)
        p = write_file(workspace / "clip.mp4", "not really a video")
        r = _read_result(Read(), file_path=p)
        assert r.success is True
        assert len(r.media) == 2
        assert all(item.artifact is not None for item in r.media)
        assert r.data["type"] == "video"
        assert r.data["frames"] == 2
        assert r.data["engine"] == "keyframe"
        # A successful read tags the source path (like image/text/pdf reads).
        assert r.resource_path == p

    def test_summary_carries_metadata_and_transcript(self, workspace, monkeypatch):
        import mote.product.toolsets.builtin.read_adapters.video as video_adapter

        async def fake(*a, **k):
            return self._fake_result(
                transcript="[00:00] hello",
                meta={
                    "title": "My Clip",
                    "duration_seconds": 12,
                    "width": 640,
                    "height": 480,
                },
            )

        monkeypatch.setattr(video_adapter, "decompose_video", fake)
        p = write_file(workspace / "clip.mp4", "not really a video")
        r = _read_result(Read(), file_path=p)
        assert "My Clip" in r.output
        assert "hello" in r.output
        assert r.data["has_transcript"] is True

    def test_no_frames_fails(self, workspace, monkeypatch):
        import mote.product.toolsets.builtin.read_adapters.video as video_adapter

        async def fake(*a, **k):
            return self._fake_result(frames=[])

        monkeypatch.setattr(video_adapter, "decompose_video", fake)
        p = write_file(workspace / "clip.mp4", "not really a video")
        with pytest.raises(ToolError, match="[Nn]o frames"):
            _read_result(Read(), file_path=p)

    def test_unavailable_raises_not_configured(self, workspace, monkeypatch):
        import mote.product.toolsets.builtin.read_adapters.video as video_adapter
        from mote.contracts.tool.errors import ToolNotConfiguredError
        from mote.runtime.media.video import VideoUnavailable

        async def fake(*a, **k):
            raise VideoUnavailable("ffmpeg is not installed. install it")

        monkeypatch.setattr(video_adapter, "decompose_video", fake)
        p = write_file(workspace / "clip.mp4", "not really a video")
        # A missing decode kernel is a configuration gap → ToolNotConfiguredError,
        # not a plain-text notice that the model would mistake for content.
        with pytest.raises(ToolNotConfiguredError, match="unavailable"):
            _read_result(Read(), file_path=p)

    def test_decode_error_fails(self, workspace, monkeypatch):
        import mote.product.toolsets.builtin.read_adapters.video as video_adapter
        from mote.runtime.media.video import VideoError

        async def fake(*a, **k):
            raise VideoError("corrupt file")

        monkeypatch.setattr(video_adapter, "decompose_video", fake)
        p = write_file(workspace / "broken.mp4", "not really a video")
        with pytest.raises(ToolError, match="broken.mp4"):
            _read_result(Read(), file_path=p)


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

    def test_visible_prior_read_still_dedups(self, workspace):
        # Bound to a Role reporting the prior read as still present => dedup stub.
        p = write_file(workspace / "a.txt", "one\ntwo\n")
        role = CapRole(resource_visible=True)
        tool = bind(Read(), role)
        first = _read(tool, file_path=p)
        assert "1→one" in first
        assert _read(tool, file_path=p) == FILE_UNCHANGED_STUB

    def test_folded_prior_read_rereads_real_content(self, workspace):
        # ContextVisibility reports the earlier read as cleared/folded => the
        # dedup short-circuit is suppressed and real content is returned again,
        # honouring reconstructable=True (a cleared read is recoverable).
        p = write_file(workspace / "a.txt", "one\ntwo\n")
        role = CapRole(resource_visible=False)
        tool = bind(Read(), role)
        first = _read(tool, file_path=p)
        assert "1→one" in first
        second = _read(tool, file_path=p)
        assert second != FILE_UNCHANGED_STUB
        assert "1→one" in second

    def test_real_content_tagged_with_resource_path(self, workspace):
        # Real reads carry the source file as resource_path so the channel can
        # stamp it onto the tool_result metadata for ContextVisibility.
        p = write_file(workspace / "a.txt", "one\n")
        result = _read_result(Read(), file_path=p)
        assert result.resource_path == p

    def test_dedup_stub_is_untagged(self, workspace):
        # The stub must NOT carry resource_path — otherwise it would register as
        # the file's latest result and mask a folded prior read.
        p = write_file(workspace / "a.txt", "one\n")
        tool = Read()
        _read(tool, file_path=p)
        stub = _read_result(tool, file_path=p)
        assert stub.output == FILE_UNCHANGED_STUB
        assert stub.resource_path is None


class TestReadRecordsState:
    def test_read_records_into_shared_state(self, workspace):
        p = write_file(workspace / "a.txt", "one\n")
        role = CapRole()
        tool = bind(Read(), role)
        _read(tool, file_path=p)
        snapshot = role.get_file_snapshot(p)
        assert snapshot is not None
        assert snapshot.version.size == os.stat(p).st_size

    def test_unbound_read_skips_recording(self, workspace):
        p = write_file(workspace / "a.txt", "one\n")
        tool = Read()  # not bound — no snapshot capabilities injected
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

    def test_non_utf8_notebook_raises_read_error(self, workspace):
        path = workspace / "encoded.ipynb"
        path.write_bytes(b"\xff\xfe")
        with pytest.raises(ToolError, match="cannot read"):
            _read(Read(), file_path=str(path))


class TestReadImage:
    def test_image_returns_media_toolresult(self, workspace):
        # 1x1 transparent PNG.
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        )
        p = os.path.join(str(workspace), "img.png")
        with open(p, "wb") as f:
            f.write(png)
        role = CapRole()
        result = _read_result(bind(Read(), role), file_path=p)
        assert isinstance(result, ToolResult)
        assert _artifact_bytes(role, result) == png
        assert result.data["type"] == "image"
        assert result.data["detail"] == "high"

    def test_large_image_is_downscaled_to_fit(self, workspace):
        from PIL import Image

        from mote.product.toolsets.constants import MAX_IMAGE_DIMENSION

        p = os.path.join(str(workspace), "big.png")
        Image.new("RGB", (4000, 2000), (123, 50, 200)).save(p)

        role = CapRole()
        result = _read_result(bind(Read(), role), file_path=p)
        assert isinstance(result, ToolResult)
        sent = _artifact_bytes(role, result)
        out = Image.open(io.BytesIO(sent))
        # Longest edge clamped to MAX_IMAGE_DIMENSION, aspect ratio preserved.
        assert max(out.size) == MAX_IMAGE_DIMENSION
        assert out.size == (MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION // 2)
        assert result.data["sent_bytes"] == len(sent)

    def test_original_detail_sends_raw_bytes(self, workspace):
        from PIL import Image

        p = os.path.join(str(workspace), "big.png")
        Image.new("RGB", (4000, 2000), (10, 20, 30)).save(p)
        with open(p, "rb") as f:
            raw = f.read()

        role = CapRole()
        result = _read_result(bind(Read(), role), file_path=p, detail="original")
        assert _artifact_bytes(role, result) == raw
        assert result.data["detail"] == "original"

    def test_small_image_high_detail_unchanged(self, workspace):
        from PIL import Image

        p = os.path.join(str(workspace), "small.png")
        Image.new("RGB", (100, 80), (1, 2, 3)).save(p)
        with open(p, "rb") as f:
            raw = f.read()

        role = CapRole()
        result = _read_result(bind(Read(), role), file_path=p, detail="high")
        assert _artifact_bytes(role, result) == raw

    def test_invalid_detail_raises(self, workspace):
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        )
        p = os.path.join(str(workspace), "img.png")
        with open(p, "wb") as f:
            f.write(png)
        with pytest.raises(ToolError, match="invalid detail"):
            _read(Read(), file_path=p, detail="low")

    def test_non_vision_model_raises(self, workspace):
        """Default model is not vision-capable → refuse before attaching the image."""
        from mote.contracts.tool.errors import ToolNotConfiguredError

        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        )
        p = os.path.join(str(workspace), "img.png")
        with open(p, "wb") as f:
            f.write(png)
        tool = bind(Read(), CapRole(default_model="gpt-4"), session_id="r_novision")
        with pytest.raises(ToolNotConfiguredError, match="not vision-capable"):
            _read_result(tool, file_path=p)


class TestReadPdf:
    _MINIMAL_PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"

    def test_non_pdf_model_raises(self, workspace):
        """Default model does not accept native PDF input → refuse up-front."""
        from mote.contracts.tool.errors import ToolNotConfiguredError

        p = os.path.join(str(workspace), "doc.pdf")
        with open(p, "wb") as f:
            f.write(self._MINIMAL_PDF)
        tool = bind(Read(), CapRole(default_model="gpt-4"), session_id="r_nopdf")
        with pytest.raises(ToolNotConfiguredError, match="native"):
            _read_result(tool, file_path=p, mode="visual")

    def test_pdf_capable_model_reads(self, workspace):
        """A PDF-capable default model reads the PDF as a document media result."""
        p = os.path.join(str(workspace), "doc.pdf")
        with open(p, "wb") as f:
            f.write(self._MINIMAL_PDF)
        tool = bind(Read(), CapRole(default_model="claude-sonnet-4"), session_id="r_pdf")
        result = _read_result(tool, file_path=p, mode="visual")
        assert isinstance(result, ToolResult)
        assert result.data["type"] == "pdf"

    def test_pdf_text_pages_preserve_boundaries(self, workspace):
        fitz = pytest.importorskip("fitz")
        path = workspace / "pages.pdf"
        document = fitz.open()
        for number in range(1, 4):
            page = document.new_page(width=300, height=200)
            page.insert_text((40, 80), f"page-{number}")
        document.save(path)
        document.close()

        result = _read_result(
            Read(),
            file_path=str(path),
            mode="text",
            pages="2-3",
        )

        assert "PDF page 2/3" in result.output
        assert "page-2" in result.output
        assert "PDF page 3/3" in result.output
        assert result.data["pages"] == [2, 3]

    def test_pdf_render_pages_are_image_media(self, workspace):
        fitz = pytest.importorskip("fitz")
        path = workspace / "pages.pdf"
        document = fitz.open()
        document.new_page(width=300, height=200)
        document.new_page(width=300, height=200)
        document.save(path)
        document.close()
        role = CapRole(default_model="claude-sonnet-4")
        tool = bind(
            Read(),
            role,
            session_id="r_pdf_render",
        )

        result = _read_result(
            tool,
            file_path=str(path),
            mode="render",
            pages="2",
        )

        assert len(result.media) == 1
        assert _artifact_bytes(role, result).startswith(b"\x89PNG")
        assert result.data["type"] == "pdf_render"
        assert result.data["pages"] == [2]
