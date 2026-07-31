#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the shared ``_video`` decompose kernel's PURE helpers.

The kernel's heavy half (download / ffmpeg / ffprobe) is external-process work
covered by the tool's integration path; here we lock down the deterministic,
side-effect-free helpers every path depends on: source/extension recognition,
time parsing, WebVTT parsing + rolling-duplicate collapse + range filtering,
frame-budget maths, and even-sampling.
"""
from __future__ import annotations

import pytest

from mote.runtime.media.video import (
    VIDEO_EXTENSIONS,
    VideoError,
    _auto_fps,
    _dedupe_cues,
    _even_indices,
    _format_transcript,
    _parse_vtt,
    _stamp,
    is_url,
    looks_like_video_path,
    parse_time,
)


class TestSourceRecognition:
    def test_http_url_is_url(self):
        assert is_url("https://x.com/v.mp4")
        assert is_url("http://x.com/v")

    def test_local_path_is_not_url(self):
        assert not is_url("/home/x/clip.mp4")
        assert not is_url("clip.mp4")

    def test_flag_and_empty_not_url(self):
        assert not is_url("-o")
        assert not is_url("")

    def test_video_extensions_recognised(self):
        for ext in ("mp4", "mkv", "webm", "mov", "avi"):
            assert ext in VIDEO_EXTENSIONS
            assert looks_like_video_path(f"/x/clip.{ext}")
            assert looks_like_video_path(f"https://x.com/clip.{ext}")

    def test_case_insensitive_extension(self):
        assert looks_like_video_path("/x/CLIP.MP4")

    def test_non_video_not_recognised(self):
        assert not looks_like_video_path("/x/photo.png")
        assert not looks_like_video_path("/x/doc.pdf")
        assert not looks_like_video_path("/x/no_extension")


class TestParseTime:
    def test_none_passthrough(self):
        assert parse_time(None) is None

    def test_numeric_passthrough(self):
        assert parse_time(90) == 90.0
        assert parse_time(1.5) == 1.5

    def test_seconds_only(self):
        assert parse_time("45") == 45.0

    def test_mm_ss(self):
        assert parse_time("1:30") == 90.0

    def test_hh_mm_ss(self):
        assert parse_time("1:02:03") == 3723.0

    def test_fractional_seconds(self):
        assert parse_time("0:01.5") == 1.5

    def test_empty_is_none(self):
        assert parse_time("") is None
        assert parse_time("   ") is None

    def test_garbage_raises(self):
        with pytest.raises(VideoError):
            parse_time("not:a:time")


class TestStamp:
    def test_mm_ss(self):
        assert _stamp(90) == "[01:30]"

    def test_h_mm_ss(self):
        assert _stamp(3723) == "[1:02:03]"

    def test_rounds(self):
        assert _stamp(89.6) == "[01:30]"


class TestDedupeCues:
    def test_identical_text_collapses(self):
        cues = [
            {"start": 0.0, "end": 1.0, "text": "hello"},
            {"start": 1.0, "end": 2.0, "text": "hello"},
        ]
        out = _dedupe_cues(cues)
        assert len(out) == 1
        assert out[0]["end"] == 2.0  # extended to the last duplicate

    def test_rolling_prefix_collapses(self):
        # YouTube auto-subs roll: "the" then "the quick" then "the quick fox".
        cues = [
            {"start": 0.0, "end": 1.0, "text": "the"},
            {"start": 1.0, "end": 2.0, "text": "the quick"},
            {"start": 2.0, "end": 3.0, "text": "the quick fox"},
        ]
        out = _dedupe_cues(cues)
        assert len(out) == 1
        assert out[0]["text"] == "the quick fox"
        assert out[0]["end"] == 3.0

    def test_distinct_cues_kept(self):
        cues = [
            {"start": 0.0, "end": 1.0, "text": "alpha"},
            {"start": 1.0, "end": 2.0, "text": "beta"},
        ]
        out = _dedupe_cues(cues)
        assert len(out) == 2


class TestParseVtt:
    def test_parses_stamped_cues(self, tmp_path):
        vtt = tmp_path / "sub.vtt"
        vtt.write_text(
            "WEBVTT\n\n"
            "00:00:00.000 --> 00:00:02.000\n"
            "Hello world\n\n"
            "00:00:02.000 --> 00:00:04.000\n"
            "Second line\n\n",
            encoding="utf-8",
        )
        cues = _parse_vtt(vtt)
        assert [c["text"] for c in cues] == ["Hello world", "Second line"]
        assert cues[0]["start"] == 0.0
        assert cues[1]["start"] == 2.0

    def test_strips_inline_tags(self, tmp_path):
        vtt = tmp_path / "sub.vtt"
        vtt.write_text(
            "WEBVTT\n\n" "00:00:00.000 --> 00:00:02.000\n" "<c>styled</c> text\n\n",
            encoding="utf-8",
        )
        cues = _parse_vtt(vtt)
        assert cues[0]["text"] == "styled text"

    def test_missing_file_returns_empty(self, tmp_path):
        assert _parse_vtt(tmp_path / "nope.vtt") == []


class TestFormatTranscript:
    _CUES = [
        {"start": 0.0, "end": 2.0, "text": "first"},
        {"start": 10.0, "end": 12.0, "text": "middle"},
        {"start": 100.0, "end": 102.0, "text": "last"},
    ]

    def test_full_range_stamped(self):
        out = _format_transcript(self._CUES, None, None)
        assert "[00:00] first" in out
        assert "[00:10] middle" in out
        assert "[01:40] last" in out

    def test_window_filters(self):
        # Only cues overlapping [5, 20] survive.
        out = _format_transcript(self._CUES, 5.0, 20.0)
        assert "middle" in out
        assert "first" not in out
        assert "last" not in out


class TestFrameBudget:
    def test_auto_fps_short_clip(self):
        fps, target = _auto_fps(20.0, max_frames=60)
        assert fps <= 2.0
        assert 1 <= target <= 60

    def test_auto_fps_respects_max_frames(self):
        _, target = _auto_fps(3600.0, max_frames=30)
        assert target <= 30

    def test_auto_fps_zero_duration(self):
        fps, target = _auto_fps(0.0, max_frames=60)
        assert fps == 1.0
        assert target == 1


class TestEvenIndices:
    def test_keeps_all_when_under_budget(self):
        assert _even_indices(3, 10) == [0, 1, 2]

    def test_first_and_last_kept(self):
        idx = _even_indices(100, 5)
        assert idx[0] == 0
        assert idx[-1] == 99
        assert len(idx) == 5

    def test_single_index(self):
        assert _even_indices(100, 1) == [0]

    def test_monotonic(self):
        idx = _even_indices(50, 7)
        assert idx == sorted(idx)
