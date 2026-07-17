#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the Read tool (``mote.executor.tools.read``).

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

from mote.common.const import TOOL_RESULT_RESOURCE_PATH
from mote.executor.tool_result import ToolError, ToolResult
from mote.executor.tools.read import FILE_UNCHANGED_STUB, Read

from .conftest import CapRole, bind, run, write_file


def _read(tool: Read, **kwargs) -> str:
    """Drive a read and return its text output.

    Read now returns real content as a ``ToolResult`` (tagged with the source
    ``resource_path``) and the dedup stub as a bare string; the executor
    normalizes both via :meth:`ToolResult.from_tool_return`. We mirror that here
    and expose ``.output`` so text assertions stay simple.
    """
    return ToolResult.from_tool_return(run(tool.call(**kwargs))).output


def _read_result(tool: Read, **kwargs) -> ToolResult:
    """Like ``_read`` but return the full normalized ToolResult (media/metadata)."""
    return ToolResult.from_tool_return(run(tool.call(**kwargs)))


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
        assert "2 lines" in out

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
        with pytest.raises(ToolError, match="not valid UTF-8"):
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


class TestReadVideo:
    """Read absorbs a LOCAL video file: frames as image media + a transcript.

    The heavy ffmpeg/ffprobe decode lives in the shared ``_video`` kernel and is
    external-process work, so these tests drive Read's own branch with
    ``decompose_video`` monkeypatched — fully offline.
    """

    def _fake_result(self, **kw):
        from mote.executor.dependency._video import VideoFrame, VideoResult

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
        import mote.executor.tools.read as rd
        from mote.executor.dependency._video import VideoFrame

        frames = [
            VideoFrame(timestamp=0.0, jpeg=b"\xff\xd8a", reason="first-frame"),
            VideoFrame(timestamp=5.0, jpeg=b"\xff\xd8b", reason="keyframe"),
        ]

        async def fake(*a, **k):
            return self._fake_result(frames=frames)

        monkeypatch.setattr(rd, "decompose_video", fake)
        p = write_file(workspace / "clip.mp4", "not really a video")
        r = _read_result(Read(), file_path=p)
        assert r.success is True
        assert len(r.images) == 2
        assert r.data["type"] == "video"
        assert r.data["frames"] == 2
        assert r.data["engine"] == "keyframe"
        # A successful read tags the source path (like image/text/pdf reads).
        assert r.resource_path == p

    def test_summary_carries_metadata_and_transcript(self, workspace, monkeypatch):
        import mote.executor.tools.read as rd

        async def fake(*a, **k):
            return self._fake_result(
                transcript="[00:00] hello",
                meta={"title": "My Clip", "duration_seconds": 12, "width": 640, "height": 480},
            )

        monkeypatch.setattr(rd, "decompose_video", fake)
        p = write_file(workspace / "clip.mp4", "not really a video")
        r = _read_result(Read(), file_path=p)
        assert "My Clip" in r.output
        assert "hello" in r.output
        assert r.data["has_transcript"] is True

    def test_no_frames_fails(self, workspace, monkeypatch):
        import mote.executor.tools.read as rd

        async def fake(*a, **k):
            return self._fake_result(frames=[])

        monkeypatch.setattr(rd, "decompose_video", fake)
        p = write_file(workspace / "clip.mp4", "not really a video")
        with pytest.raises(ToolError, match="[Nn]o frames"):
            _read_result(Read(), file_path=p)

    def test_unavailable_raises_not_configured(self, workspace, monkeypatch):
        import mote.executor.tools.read as rd
        from mote.common.exception import ToolNotConfiguredError
        from mote.executor.dependency._video import VideoUnavailable

        async def fake(*a, **k):
            raise VideoUnavailable("ffmpeg is not installed. install it")

        monkeypatch.setattr(rd, "decompose_video", fake)
        p = write_file(workspace / "clip.mp4", "not really a video")
        # A missing decode kernel is a configuration gap → ToolNotConfiguredError,
        # not a plain-text notice that the model would mistake for content.
        with pytest.raises(ToolNotConfiguredError, match="unavailable"):
            _read_result(Read(), file_path=p)

    def test_decode_error_fails(self, workspace, monkeypatch):
        import mote.executor.tools.read as rd
        from mote.executor.dependency._video import VideoError

        async def fake(*a, **k):
            raise VideoError("corrupt file")

        monkeypatch.setattr(rd, "decompose_video", fake)
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
        result = _read_result(Read(), file_path=p)
        assert isinstance(result, ToolResult)
        assert result.images and isinstance(result.images[0], str)
        # A 1x1 image is within MAX_IMAGE_DIMENSION, so it's sent unchanged and
        # the embedded base64 round-trips to the original bytes.
        assert base64.b64decode(result.images[0]) == png
        assert result.data["type"] == "image"
        assert result.data["detail"] == "high"

    def test_large_image_is_downscaled_to_fit(self, workspace):
        from PIL import Image

        from mote.common.const.tools import MAX_IMAGE_DIMENSION

        p = os.path.join(str(workspace), "big.png")
        Image.new("RGB", (4000, 2000), (123, 50, 200)).save(p)

        result = _read_result(Read(), file_path=p)
        assert isinstance(result, ToolResult)
        out = Image.open(io.BytesIO(base64.b64decode(result.images[0])))
        # Longest edge clamped to MAX_IMAGE_DIMENSION, aspect ratio preserved.
        assert max(out.size) == MAX_IMAGE_DIMENSION
        assert out.size == (MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION // 2)
        assert result.data["sent_bytes"] == len(base64.b64decode(result.images[0]))

    def test_original_detail_sends_raw_bytes(self, workspace):
        from PIL import Image

        p = os.path.join(str(workspace), "big.png")
        Image.new("RGB", (4000, 2000), (10, 20, 30)).save(p)
        with open(p, "rb") as f:
            raw = f.read()

        result = _read_result(Read(), file_path=p, detail="original")
        assert base64.b64decode(result.images[0]) == raw
        assert result.data["detail"] == "original"

    def test_small_image_high_detail_unchanged(self, workspace):
        from PIL import Image

        p = os.path.join(str(workspace), "small.png")
        Image.new("RGB", (100, 80), (1, 2, 3)).save(p)
        with open(p, "rb") as f:
            raw = f.read()

        result = _read_result(Read(), file_path=p, detail="high")
        assert base64.b64decode(result.images[0]) == raw

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
        from mote.common.exception import ToolNotConfiguredError

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
        from mote.common.exception import ToolNotConfiguredError

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
