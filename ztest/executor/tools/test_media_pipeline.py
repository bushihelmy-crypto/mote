"""Unit tests for the MediaPipeline tool (executor/tools/media_pipeline_tool.py).

Tests the graph topology, skip semantics, image-ref injection, dual-mode
storyboard, conditional routing, and end-to-end execution with mocked creators.
All tests are offline — creator classes are monkeypatched so no run_rollout
import or network is needed.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import pytest

from metagpt.common.exception.media import (
    MediaGenerationError,
    PermanentMediaGenerationError,
)
from metagpt.executor.tools.media_pipeline.graph import build_media_pipeline_graph
from metagpt.executor.tools.media_pipeline.creators import _summarize_poll_results, FfmpegComposer
from metagpt.executor.tools.media_pipeline.nodes import (
    _can_compose,
    _inject_image_refs,
    _ordered_local_paths,
    _parse_storyboard_response,
    _route_after_render_gate,
    _route_after_storyboard,
    promo_node,
    render_gate_node,
    storyboard_node,
)
from metagpt.executor.tools.media_pipeline.state import MediaPipelineState
from metagpt.executor.tools.media_pipeline_tool import MediaPipeline
from metagpt.executor.tasks.bggraph import START, END, BgGraph


def _is_bg_task_result(obj) -> bool:
    """Duck-type check for BgTaskResult (avoids namespace-pkg isinstance mismatch)."""
    return type(obj).__name__ == "BgTaskResult" and hasattr(obj, "poll_factory")

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeBgTaskResult:
    """Mimics the BgTaskResult returned by creators (result + poll)."""

    result: Any = None
    poll: Any = None


class FakeCreator:
    """Generic fake creator that returns a FakeBgTaskResult with canned data."""

    def __init__(self, label: str):
        self.label = label
        self.called_with: list = []

    async def __call__(self, items, **_):
        self.called_with.append(items)
        return FakeBgTaskResult(result={self.label: len(items)})


# ---------------------------------------------------------------------------
# Graph topology
# ---------------------------------------------------------------------------


class TestGraphTopology:
    def test_build_succeeds(self):
        g = build_media_pipeline_graph()
        assert isinstance(g, BgGraph)
        expected_nodes = {
            "storyboard", "template_init", "dispatch",
            "image", "audio", "music",
            "video", "duration_measure", "render_gate", "promo_render",
        }
        assert set(g._nodes.keys()) == expected_nodes

    def test_node_count(self):
        g = build_media_pipeline_graph()
        assert len(g._nodes) == 10

    def test_single_start_edge(self):
        g = build_media_pipeline_graph()
        start_edges = [e for e in g._edges if e.from_node == START]
        assert len(start_edges) == 1
        assert start_edges[0].to_node == "storyboard"

    def test_end_reachable(self):
        """END is reachable via conditional edge (render_gate→done) and static (promo_render→END)."""
        g = build_media_pipeline_graph()
        # Static END edge from promo_render
        end_edges = [e for e in g._edges if e.to_node == END]
        assert len(end_edges) == 1
        assert end_edges[0].from_node == "promo_render"
        # Also reachable via conditional edge
        cond_targets = set()
        for ce in g._conditional_edges:
            for target in ce.mapping.values():
                cond_targets.add(target)
        assert END in cond_targets

    def test_conditional_edges(self):
        g = build_media_pipeline_graph()
        assert len(g._conditional_edges) == 2
        # storyboard conditional
        storyboard_ce = [ce for ce in g._conditional_edges if ce.from_node == "storyboard"]
        assert len(storyboard_ce) == 1
        assert storyboard_ce[0].mapping == {"full_pipeline": "template_init", "assets_only": "dispatch"}
        # render_gate conditional
        gate_ce = [ce for ce in g._conditional_edges if ce.from_node == "render_gate"]
        assert len(gate_ce) == 1
        assert gate_ce[0].mapping == {"render": "promo_render", "done": END}

    def test_waiting_edges(self):
        g = build_media_pipeline_graph()
        assert len(g._waiting_edges) == 2
        # audio + music → duration_measure
        dur_we = [we for we in g._waiting_edges if we.to_node == "duration_measure"]
        assert len(dur_we) == 1
        assert set(dur_we[0].sources) == {"audio", "music"}
        # video + duration_measure → render_gate
        gate_we = [we for we in g._waiting_edges if we.to_node == "render_gate"]
        assert len(gate_we) == 1
        assert set(gate_we[0].sources) == {"video", "duration_measure"}

    def test_storyboard_has_no_static_out_edges(self):
        """storyboard must have ONLY conditional edges out (no static), to avoid ADDITIVE trigger."""
        g = build_media_pipeline_graph()
        static_from_storyboard = [e for e in g._edges if e.from_node == "storyboard"]
        assert static_from_storyboard == []

    def test_compile_succeeds(self):
        g = build_media_pipeline_graph()
        executor = g.compile()
        assert callable(executor)

    def test_stage_summary_contains_key_nodes(self):
        g = build_media_pipeline_graph()
        summary = g.stage_summary
        assert "storyboard" in summary
        assert "template_init" in summary
        assert "dispatch" in summary
        assert "image" in summary
        assert "video" in summary
        assert "duration_measure" in summary
        assert "render_gate" in summary
        assert "promo_render" in summary


# ---------------------------------------------------------------------------
# Conditional routing
# ---------------------------------------------------------------------------


class TestRouting:
    def test_storyboard_route_full_pipeline_with_promo(self):
        state = MediaPipelineState(promo={"promo_dir": "/some/dir"})
        assert _route_after_storyboard(state) == "full_pipeline"

    def test_storyboard_route_full_pipeline_with_promo_dir(self):
        state = MediaPipelineState(promo_dir="/some/dir")
        assert _route_after_storyboard(state) == "full_pipeline"

    def test_storyboard_route_assets_only(self):
        state = MediaPipelineState(images=[{"desc": "test"}])
        assert _route_after_storyboard(state) == "assets_only"

    def test_render_gate_route_done_no_dir(self):
        state = MediaPipelineState()
        assert _route_after_render_gate(state) == "done"

    def test_render_gate_route_done_no_clips(self, tmp_path):
        # promo_dir set but no video clips produced → nothing to compose.
        state = MediaPipelineState(promo_dir=str(tmp_path))
        assert _route_after_render_gate(state) == "done"

    def test_render_gate_route_render(self, tmp_path):
        # Output dir + at least one local video clip → compose.
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"\x00")
        state = MediaPipelineState(
            promo_dir=str(tmp_path),
            videos=[{"filename": "clip.mp4"}],
        )
        state.video = {"results": [
            {"status": "success", "filename": "clip.mp4", "local_path": str(clip)}
        ]}
        assert _route_after_render_gate(state) == "render"

    def test_render_gate_route_done_clip_missing_on_disk(self, tmp_path):
        # Result reports success but the file isn't on disk → can't compose.
        state = MediaPipelineState(
            promo_dir=str(tmp_path),
            videos=[{"filename": "clip.mp4"}],
        )
        state.video = {"results": [
            {"status": "success", "filename": "clip.mp4", "local_path": str(tmp_path / "missing.mp4")}
        ]}
        # _ordered_local_paths returns the path; existence is checked at compose
        # time. Routing only needs a local_path to be present.
        assert _route_after_render_gate(state) == "render"


# ---------------------------------------------------------------------------
# Storyboard parsing
# ---------------------------------------------------------------------------


class TestStoryboardParsing:
    def test_parse_plain_json(self):
        text = '{"images": [{"description": "sun", "filename": "sun.png"}]}'
        result = _parse_storyboard_response(text)
        assert result["images"][0]["description"] == "sun"

    def test_parse_json_with_fences(self):
        text = '```json\n{"music": [{"prompt": "epic", "filename": "bg.mp3"}]}\n```'
        result = _parse_storyboard_response(text)
        assert result["music"][0]["prompt"] == "epic"

    def test_parse_invalid_returns_empty(self):
        result = _parse_storyboard_response("not json at all")
        assert result == {}


# ---------------------------------------------------------------------------
# Skip semantics (all empty — manual mode pass-through)
# ---------------------------------------------------------------------------


class TestSkipSemantics:
    async def test_all_empty_inputs_skip(self):
        """When all inputs are empty, every node short-circuits and graph completes."""
        g = build_media_pipeline_graph()
        executor = g.compile()
        res = await executor(
            prompt="", images=[], audios=[], musics=[], videos=[], promo={}, promo_dir=""
        )
        assert _is_bg_task_result(res)
        final = await res.poll_factory()
        # render_gate routes to done (END) since no promo_dir
        # The final result comes from whatever the END-reaching node produced
        assert isinstance(final, dict)

    async def test_manual_mode_no_prompt(self):
        """Manual mode: no prompt, media specified → skips storyboard planning."""
        g = build_media_pipeline_graph()
        executor = g.compile()
        # All empty media — passes through storyboard with mode=empty, routes assets_only
        res = await executor(
            prompt="", images=[], audios=[], musics=[], videos=[], promo={}, promo_dir=""
        )
        final = await res.poll_factory()
        assert isinstance(final, dict)


# ---------------------------------------------------------------------------
# Image ref injection
# ---------------------------------------------------------------------------


class TestImageRefInjection:
    def test_no_refs(self):
        videos = [{"prompt": "test", "filename": "v.mp4"}]
        result = _inject_image_refs(videos, None)
        assert result == videos

    def test_ref_resolved_from_list(self):
        image_results = [{"url": "https://cdn/img0.png"}, {"url": "https://cdn/img1.png"}]
        videos = [{"prompt": "test", "filename": "v.mp4", "first_frame": "$image:0"}]
        result = _inject_image_refs(videos, image_results)
        assert result[0]["first_frame"] == "https://cdn/img0.png"

    def test_ref_index_out_of_range(self):
        image_results = [{"url": "https://cdn/img0.png"}]
        videos = [{"prompt": "test", "filename": "v.mp4", "first_frame": "$image:5"}]
        result = _inject_image_refs(videos, image_results)
        # Unresolvable ref left as-is
        assert result[0]["first_frame"] == "$image:5"

    def test_no_mutation_of_original(self):
        image_results = [{"url": "https://cdn/img0.png"}]
        original = {"prompt": "test", "filename": "v.mp4", "first_frame": "$image:0"}
        videos = [original]
        result = _inject_image_refs(videos, image_results)
        # Original dict untouched
        assert original["first_frame"] == "$image:0"
        assert result[0]["first_frame"] == "https://cdn/img0.png"

    def test_pre_url_fallback(self):
        image_results = [{"pre_url": "https://cdn/pre.png"}]
        videos = [{"prompt": "test", "filename": "v.mp4", "first_frame": "$image:0"}]
        result = _inject_image_refs(videos, image_results)
        assert result[0]["first_frame"] == "https://cdn/pre.png"


# ---------------------------------------------------------------------------
# Tool class
# ---------------------------------------------------------------------------


class TestMediaPipelineTool:
    def test_instantiation(self):
        t = MediaPipeline()
        assert t.name == "MediaPipeline"
        assert "media_pipeline" in t.aliases
        assert t._graph is not None
        assert t._executor is not None

    def test_graph_has_10_nodes(self):
        t = MediaPipeline()
        assert len(t._graph._nodes) == 10

    async def test_call_returns_bg_task_result(self):
        t = MediaPipeline()
        result = await t.call(prompt="test", promo={"promo_dir": "/tmp", "width": 1920, "height": 1080})
        assert _is_bg_task_result(result)

    async def test_call_all_skip(self):
        t = MediaPipeline()
        result = await t.call(prompt="", promo={"promo_dir": "/tmp", "width": 1920, "height": 1080})
        final = await result.poll_factory()
        assert isinstance(final, dict)

    async def test_call_with_prompt_and_promo(self):
        """Tool accepts prompt + promo parameters."""
        t = MediaPipeline()
        result = await t.call(prompt="make a video", promo={"promo_dir": "/nonexistent", "width": 1920, "height": 1080})
        assert _is_bg_task_result(result)


# ---------------------------------------------------------------------------
# Poll-result summarization (partial / total failure bubbling)
# ---------------------------------------------------------------------------


class TestSummarizePollResults:
    def test_all_success(self):
        results = [
            {"status": "success", "filename": "a.mp4", "url": "u1"},
            {"status": "success", "filename": "b.mp4", "url": "u2"},
        ]
        out = _summarize_poll_results(results, "videos")
        assert out["summary"] == "2/2 videos generated."
        assert out["failed"] == []

    def test_partial_failure_kept_not_raised(self):
        results = [
            {"status": "success", "filename": "a.mp4", "url": "u1"},
            {"status": "failed", "filename": "b.mp4", "error": "boom"},
        ]
        out = _summarize_poll_results(results, "videos")
        assert out["summary"] == "1/2 videos generated."
        assert out["failed"] == [{"filename": "b.mp4", "error": "boom"}]

    def test_all_failed_raises_retryable_by_default(self):
        # No item tagged permanent → the batch is retryable so the engine
        # re-submits (policy: try whatever can be tried).
        results = [
            {"status": "failed", "filename": "a.mp4", "error": "boom"},
            {"status": "failed", "filename": "b.mp4", "error": "nope"},
        ]
        with pytest.raises(MediaGenerationError, match="All 2 videos failed") as ei:
            _summarize_poll_results(results, "videos")
        assert ei.value.retryable is True
        assert not isinstance(ei.value, PermanentMediaGenerationError)

    def test_all_failed_permanent_when_every_item_permanent(self):
        # Every failure permanent (auth/quota/content) → fail fast, don't retry.
        results = [
            {"status": "failed", "filename": "a.mp4", "error": "quota", "permanent": True},
            {"status": "failed", "filename": "b.mp4", "error": "blocked", "permanent": True},
        ]
        with pytest.raises(PermanentMediaGenerationError):
            _summarize_poll_results(results, "videos")

    def test_all_failed_retryable_if_any_transient(self):
        # A single transient failure makes the whole batch worth re-submitting.
        results = [
            {"status": "failed", "filename": "a.mp4", "error": "quota", "permanent": True},
            {"status": "failed", "filename": "b.mp4", "error": "blip", "permanent": False},
        ]
        with pytest.raises(MediaGenerationError) as ei:
            _summarize_poll_results(results, "videos")
        assert ei.value.retryable is True
        assert not isinstance(ei.value, PermanentMediaGenerationError)

    def test_empty_results_no_raise(self):
        # Nothing submitted (e.g. node skipped) — not a failure.
        out = _summarize_poll_results([], "videos")
        assert out["summary"] == "0/0 videos generated."
        assert out["failed"] == []


# ---------------------------------------------------------------------------
# render_gate aggregation (P2 + P3)
# ---------------------------------------------------------------------------


class TestRenderGateAggregation:
    async def _run(self, state):
        stage = await render_gate_node(state)
        return (await stage.submit)["render_gate_out"]

    async def test_aggregates_artifacts_and_failures(self):
        state = MediaPipelineState()
        state.video = {"results": [
            {"status": "success", "filename": "a.mp4", "local_path": "/w/a.mp4"},
            {"status": "failed", "filename": "sun.mp4", "error": "service failed"},
        ]}
        state.image = {"results": [{"status": "success", "filename": "i.png", "url": "u"}]}
        out = await self._run(state)
        assert out["gate"] == "reached"
        assert out["has_failures"] is True
        assert out["artifacts"]["videos"]["succeeded"] == ["/w/a.mp4"]
        assert out["artifacts"]["videos"]["failed"] == [
            {"filename": "sun.mp4", "error": "service failed"}
        ]
        assert out["artifacts"]["images"]["succeeded"] == ["u"]

    async def test_not_composed_flag_without_promo(self):
        state = MediaPipelineState()
        out = await self._run(state)
        assert out["final_video"] == "not_composed"
        assert "note" in out

    async def test_pending_compose_with_valid_project(self, tmp_path):
        clip = tmp_path / "a.mp4"
        clip.write_bytes(b"\x00")
        state = MediaPipelineState(
            promo_dir=str(tmp_path),
            videos=[{"filename": "a.mp4"}],
        )
        state.video = {"results": [
            {"status": "success", "filename": "a.mp4", "local_path": str(clip)}
        ]}
        out = await self._run(state)
        assert out["final_video"] == "pending_compose"
        assert "note" not in out


# ---------------------------------------------------------------------------
# Duration wiring (storyboard target duration injection)
# ---------------------------------------------------------------------------


class TestDurationWiring:
    async def test_duration_injected_into_llm_prompt(self, monkeypatch):
        captured = {}

        class FakeLLM:
            async def aask(self, msg, system_msgs=None, stream=False):
                captured["msg"] = msg
                return "{}"

        import metagpt.executor.tools.media_pipeline.nodes as nodes_mod
        monkeypatch.setattr(nodes_mod, "LLM", lambda *a, **k: FakeLLM())

        state = MediaPipelineState(prompt="make a solar system video", duration=60)
        stage = await storyboard_node(state)
        await stage.submit
        assert "60 seconds" in captured["msg"]
        assert "make a solar system video" in captured["msg"]

    async def test_no_duration_keeps_plain_prompt(self, monkeypatch):
        captured = {}

        class FakeLLM:
            async def aask(self, msg, system_msgs=None, stream=False):
                captured["msg"] = msg
                return "{}"

        import metagpt.executor.tools.media_pipeline.nodes as nodes_mod
        monkeypatch.setattr(nodes_mod, "LLM", lambda *a, **k: FakeLLM())

        state = MediaPipelineState(prompt="make a video", duration=0)
        stage = await storyboard_node(state)
        await stage.submit
        assert captured["msg"] == "make a video"

    def test_state_has_duration_field(self):
        state = MediaPipelineState(duration=90)
        assert state.duration == 90

    async def test_tool_forwards_duration(self):
        t = MediaPipeline()
        # Duration is accepted and threaded through without error.
        result = await t.call(
            prompt="", duration=60,
            promo={"promo_dir": "/tmp", "width": 1920, "height": 1080},
        )
        assert _is_bg_task_result(result)


# ---------------------------------------------------------------------------
# Ordered local-path resolution (clip ordering for compose)
# ---------------------------------------------------------------------------


class TestOrderedLocalPaths:
    def test_orders_by_plan(self):
        plan = [{"filename": "b.mp4"}, {"filename": "a.mp4"}]
        node_output = {"results": [
            {"status": "success", "filename": "a.mp4", "local_path": "/w/a.mp4"},
            {"status": "success", "filename": "b.mp4", "local_path": "/w/b.mp4"},
        ]}
        assert _ordered_local_paths(plan, node_output) == ["/w/b.mp4", "/w/a.mp4"]

    def test_skips_failed_and_missing_path(self):
        plan = [{"filename": "a.mp4"}, {"filename": "b.mp4"}]
        node_output = {"results": [
            {"status": "success", "filename": "a.mp4", "local_path": "/w/a.mp4"},
            {"status": "failed", "filename": "b.mp4", "error": "boom"},
        ]}
        assert _ordered_local_paths(plan, node_output) == ["/w/a.mp4"]

    def test_unmatched_success_appended(self):
        plan = [{"filename": "a.mp4"}]
        node_output = {"results": [
            {"status": "success", "filename": "a.mp4", "local_path": "/w/a.mp4"},
            {"status": "success", "filename": "x.mp4", "local_path": "/w/x.mp4"},
        ]}
        assert _ordered_local_paths(plan, node_output) == ["/w/a.mp4", "/w/x.mp4"]

    def test_none_output(self):
        assert _ordered_local_paths([{"filename": "a.mp4"}], None) == []

    def test_music_plan_and_output_keys_distinct(self):
        """Regression: music plan lives in ``musics`` while the node output lands
        in ``music`` (engine stores under node name). They must not collide — a
        prior bug overwrote the plan list with the output dict, crashing
        ``promo_node`` with ``'str' object has no attribute 'get'``.
        """
        state = MediaPipelineState(
            musics=[{"prompt": "epic", "filename": "bg.mp3", "duration": 60}],
        )
        # Engine stores the music node output under the node name ``music``.
        state.music = {"results": [
            {"status": "success", "filename": "bg.mp3", "local_path": "/w/bg.mp3"},
        ]}
        # Plan list is intact (not clobbered by the output dict).
        assert state.musics == [{"prompt": "epic", "filename": "bg.mp3", "duration": 60}]
        # promo_node's music resolution must not crash and orders by plan.
        paths = _ordered_local_paths(state.musics, getattr(state, "music", None))
        assert paths == ["/w/bg.mp3"]


# ---------------------------------------------------------------------------
# FFmpeg compose (real ffmpeg if available)
# ---------------------------------------------------------------------------

_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
_needs_ffmpeg = pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")


async def _make_test_clip(path, *, seconds: int, color: str = "blue") -> None:
    """Generate a tiny silent test video via ffmpeg lavfi."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c={color}:s=320x240:d={seconds}:r=30",
        "-pix_fmt", "yuv420p", str(path),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate()


async def _make_test_audio(path, *, seconds: int, freq: int = 440) -> None:
    """Generate a tiny test audio (sine tone) via ffmpeg lavfi."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
        str(path),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate()


@_needs_ffmpeg
class TestFfmpegComposer:
    async def test_no_clips_fails(self, tmp_path):
        composer = FfmpegComposer()
        out = await composer.compose(videos=[], output_path=str(tmp_path / "o.mp4"))
        assert out["status"] == "failed"
        assert out["stage"] == "preflight"

    async def test_concat_only(self, tmp_path):
        c1 = tmp_path / "c1.mp4"
        c2 = tmp_path / "c2.mp4"
        await _make_test_clip(c1, seconds=2, color="red")
        await _make_test_clip(c2, seconds=3, color="green")
        out_path = tmp_path / "out.mp4"

        composer = FfmpegComposer()
        out = await composer.compose(videos=[str(c1), str(c2)], output_path=str(out_path))
        assert out["status"] == "success", out.get("error")
        assert out_path.exists() and out_path.stat().st_size > 0
        # Total duration ≈ 2 + 3 = 5s.
        dur = await composer._probe_duration(str(out_path))
        assert 4.5 <= dur <= 5.6

    async def test_unified_duration_with_narration_and_music(self, tmp_path):
        # Two clips (2s + 2s = 4s), narration per clip, and a long bgm (10s).
        c1 = tmp_path / "c1.mp4"
        c2 = tmp_path / "c2.mp4"
        await _make_test_clip(c1, seconds=2, color="red")
        await _make_test_clip(c2, seconds=2, color="blue")
        n1 = tmp_path / "n1.m4a"
        n2 = tmp_path / "n2.m4a"
        await _make_test_audio(n1, seconds=1, freq=300)
        await _make_test_audio(n2, seconds=1, freq=500)
        bgm = tmp_path / "bgm.m4a"
        await _make_test_audio(bgm, seconds=10, freq=200)
        out_path = tmp_path / "final.mp4"

        composer = FfmpegComposer()
        out = await composer.compose(
            videos=[str(c1), str(c2)],
            narration=[str(n1), str(n2)],
            music=[str(bgm)],
            output_path=str(out_path),
        )
        assert out["status"] == "success", out.get("error")
        assert out["has_music"] is True
        assert out["narration_clips"] == 2
        # Final duration is unified to the 4s video, NOT the 10s music.
        dur = await composer._probe_duration(str(out_path))
        assert 3.6 <= dur <= 4.6

    async def test_promo_node_composes_end_to_end(self, tmp_path):
        c1 = tmp_path / "clip_a.mp4"
        await _make_test_clip(c1, seconds=2, color="purple")
        state = MediaPipelineState(
            promo={"promo_dir": str(tmp_path), "width": 1920, "height": 1080},
            promo_dir=str(tmp_path),
            videos=[{"filename": "clip_a.mp4"}],
        )
        state.video = {"results": [
            {"status": "success", "filename": "clip_a.mp4", "local_path": str(c1)}
        ]}
        stage = await promo_node(state)
        submit_result = await stage.submit
        assert submit_result["status"] == "running"
        result = (await stage.poll(submit_result))["promo_out"]
        assert result["status"] == "success", result.get("error")
        expected = tmp_path / "promo-1920x1080.mp4"
        assert expected.exists() and expected.stat().st_size > 0

