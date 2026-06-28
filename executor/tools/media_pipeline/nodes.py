"""Node functions for the media production pipeline graph.

Each node follows the ``async def xxx(state) -> Stage`` contract. Internally they
instantiate the corresponding creator and invoke its batch-generation method.
The creator returns a ``BgTaskResult`` whose ``.poll_factory`` is the background
polling factory — we chain it through our Stage's poll phase so the graph engine
drives the full lifecycle (submit → poll → store result).

Creators are imported lazily (inside the node function) to avoid import-time
dependency on config when this module is only being scanned for registration.
"""
from __future__ import annotations

import json
import os

from metagpt.common.logs import logger
from metagpt.executor.tasks.bggraph import Stage

from .state import MediaPipelineState
from metagpt.executor.tools.media_pipeline.creators import ImageCreator
from metagpt.executor.tools.media_pipeline.creators import AudioCreator
from metagpt.executor.tools.media_pipeline.creators import MusicCreator
from metagpt.executor.tools.media_pipeline.creators import VideoCreator
from metagpt.router import LLM
from metagpt.executor.tools.media_pipeline.creators import FfmpegComposer

# ---------------------------------------------------------------------------
# Storyboard LLM prompt
# ---------------------------------------------------------------------------

_STORYBOARD_SYSTEM_PROMPT = """\
You are a video production planner. Given a user request, output a JSON object
describing the media assets to produce. The schema:

{
  "images": [{"description": str, "filename": str, "style"?: str, "size"?: str}],
  "audios": [{"text": str, "filename": str, "gender"?: str}],
  "music": [{"prompt": str, "filename": str}],
  "videos": [{"prompt": str, "filename": str, "seconds"?: int, "first_frame"?: str, "style"?: str}],
  "promo": {"promo_dir"?: str, "width"?: int, "height"?: int, "source_orientation"?: str},
  "shots": [{"description": str, "duration_seconds": float}]
}

Rules:
- Only include non-empty arrays/objects for what the request actually needs.
- Filenames should be descriptive snake_case with proper extensions.
- Output ONLY valid JSON, no markdown fences or commentary.

Images are MANDATORY for any video (CRITICAL — do not skip):
- Whenever you plan ANY "videos", you MUST also plan a matching "images" entry for
  EACH video clip. Images are the visual foundation: every video clip is animated
  from a generated still image used as its first frame. A video with no image has
  nothing to animate and will produce no visual output.
- The "images" array length should be >= the "videos" array length. As a rule,
  plan exactly one image per video clip (one image → one clip), in the same order.
- For EACH video clip, set its "first_frame" to "$image:<index>" pointing at the
  matching image (e.g. the 1st clip uses "$image:0", the 2nd uses "$image:1", ...).
  The "<index>" is the 0-based position of the image in the "images" array.
- Each image "description" must be a rich, standalone visual prompt (subject,
  composition, lighting, style, mood) — it is sent directly to an image model.
- The ONLY time "images" may be empty is when the request explicitly asks for no
  visuals at all (e.g. audio-only / podcast). For any normal promo/explainer/
  story video, "images" must be non-empty.

A complete video needs visuals + narration + music (CRITICAL — do not skip):
- "videos": REQUIRED for any video request. Plan one clip per shot/scene so the
  clips concatenate into the finished video. Empty "videos" means no moving
  picture is produced — never leave it empty for a video request.
- "audios" (narration): REQUIRED unless the user explicitly asks for no voiceover.
  Plan one narration entry per shot, paced to that shot's duration, so the spoken
  track covers the whole video. The user almost always wants narration.
- "music" (background music): REQUIRED unless the user explicitly asks for silence
  or no music. Plan at least one background track that fits the video's mood and
  spans its full length. Background music is expected by default.
- In short: for a normal promo/explainer/story video, ALL of "images", "videos",
  "audios", and "music" must be non-empty and consistent in count/order.

Duration planning (IMPORTANT):
- When a target total duration is given, plan all timing so the assets add up to
  that total. Do NOT leave durations to defaults.
- Each video clip's "seconds" must be set explicitly; the sum of all video clip
  "seconds" should be close to the target total duration.
- Narration ("audios") text length should be paced to fit its shot's seconds.
- Reflect the per-shot timing in "shots" with "duration_seconds" that sum to the
  target total.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _noop():
    """No-op submit for skipped nodes (empty input)."""
    return {}


def _noop_result(result: dict):
    """Return a coroutine that yields a fixed result dict."""

    async def _inner():
        return result

    return _inner()


def _assets_dir(state: MediaPipelineState, subdir: str) -> str | None:
    """Resolve output directory for generated assets.

    If promo_dir is set, saves to {promo_dir}/public/{subdir}/ so Remotion can
    reference them via staticFile(). Otherwise returns None (no local save).
    """
    promo_dir = state.promo_dir or state.promo.get("promo_dir", "")
    if not promo_dir:
        return None
    path = os.path.join(promo_dir, "public", subdir)
    os.makedirs(path, exist_ok=True)
    return path


def _parse_storyboard_response(text: str) -> dict:
    """Best-effort parse of LLM storyboard JSON response."""
    # Strip markdown code fences if present
    text = text.strip()
    if text.startswith("```"):
        # Remove opening fence (possibly ```json)
        first_nl = text.index("\n")
        text = text[first_nl + 1:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------------------
# Router functions (conditional edge logic)
# ---------------------------------------------------------------------------

def _route_after_storyboard(state: MediaPipelineState) -> str:
    """Route after storyboard: full_pipeline if promo needed, else assets_only."""
    has_promo = bool(state.promo) or bool(state.promo_dir)
    return "full_pipeline" if has_promo else "assets_only"


def _route_after_render_gate(state: MediaPipelineState) -> str:
    """Route after render_gate: compose with FFmpeg if clips exist, else done."""
    if _can_compose(state):
        return "render"
    return "done"


def _success_results(node_output) -> list[dict]:
    """Return the successful result entries of a creator node output."""
    results = node_output.get("results", []) if isinstance(node_output, dict) else []
    return [r for r in results if isinstance(r, dict) and r.get("status") == "success"]


def _ordered_local_paths(plan: list[dict], node_output) -> list[str]:
    """Local paths of a node's successful artifacts, ordered by the input plan.

    Matches result entries to the storyboard ``plan`` by filename so clip order
    follows the storyboard. Unmatched successes are appended in result order.
    """
    success = _success_results(node_output)
    by_name = {r.get("filename"): r.get("local_path") for r in success if r.get("local_path")}
    ordered: list[str] = []
    used: set[str] = set()
    for item in plan or []:
        fn = item.get("filename")
        if fn in by_name:
            ordered.append(by_name[fn])
            used.add(fn)
    for r in success:
        fn = r.get("filename")
        lp = r.get("local_path")
        if lp and fn not in used:
            ordered.append(lp)
            used.add(fn)
    return ordered


def _can_compose(state: MediaPipelineState) -> bool:
    """True when there is an output dir and at least one local video clip.

    FFmpeg composition needs a destination (promo_dir) and downloaded clips
    (creators only save locally when promo_dir is set).
    """
    promo_dir = state.promo_dir or state.promo.get("promo_dir", "")
    if not promo_dir:
        return False
    return bool(_ordered_local_paths(state.videos, getattr(state, "video", None)))


# ---------------------------------------------------------------------------
# Storyboard (entry — LLM planning or pass-through)
# ---------------------------------------------------------------------------


async def storyboard_node(state: MediaPipelineState) -> Stage:
    """Plan media production from a natural-language prompt (or pass-through).

    Params:
        prompt: $input.prompt — 自然语言请求（空则跳过规划）
    """
    has_media = state.images or state.audios or state.musics or state.videos
    if not state.prompt and has_media:
        # Manual mode — media already specified, skip planning
        return Stage(submit=_noop_result({"storyboard": {"mode": "manual"}}))
    if not state.prompt:
        # Empty mode — nothing to do, pass through
        return Stage(submit=_noop_result({"storyboard": {"mode": "empty"}}))

    # Auto mode — use LLM to plan
    async def submit():

        llm = LLM()
        user_msg = state.prompt
        if state.duration:
            user_msg = (
                f"{state.prompt}\n\n"
                f"Target total video duration: {state.duration} seconds. "
                f"Plan all clip/music/narration timing so the assets add up to "
                f"this total."
            )
        response = await llm.aask(
            msg=user_msg,
            system_msgs=[_STORYBOARD_SYSTEM_PROMPT],
            stream=False,
        )
        plan = _parse_storyboard_response(response)
        # Populate state fields from plan
        if plan.get("images"):
            state.images = plan["images"]
        if plan.get("audios"):
            state.audios = plan["audios"]
        if plan.get("music"):
            state.musics = plan["music"]
        if plan.get("videos"):
            state.videos = plan["videos"]
        if plan.get("promo"):
            state.promo = plan["promo"]
            if plan["promo"].get("promo_dir"):
                state.promo_dir = plan["promo"]["promo_dir"]
        state.storyboard = plan
        return {"storyboard": plan}

    return Stage(submit=submit())


# ---------------------------------------------------------------------------
# Output init (resolve + prepare the output directory)
# ---------------------------------------------------------------------------


async def template_init_node(state: MediaPipelineState) -> Stage:
    """Resolve and prepare the output directory for the final composed video.

    Params:
        promo_dir: $input.promo_dir — 输出目录
        promo: $input.promo — 渲染参数（备选 promo_dir 来源）
    """

    async def submit():
        promo_dir = state.promo_dir or state.promo.get("promo_dir", "")
        state.promo_dir = promo_dir
        if not promo_dir:
            return {"template_init_out": {"status": "no_dir"}}
        try:
            os.makedirs(promo_dir, exist_ok=True)
        except OSError as e:
            return {"template_init_out": {"status": "error", "promo_dir": promo_dir, "error": str(e)}}
        return {"template_init_out": {"status": "ready", "promo_dir": promo_dir}}

    return Stage(submit=submit())


# ---------------------------------------------------------------------------
# Dispatch (entry fan-out)
# ---------------------------------------------------------------------------


async def dispatch_node(state: MediaPipelineState) -> Stage:
    """Fan-out entry point — immediately completes so parallel nodes can start."""
    return Stage(submit=_noop())


# ---------------------------------------------------------------------------
# Image
# ---------------------------------------------------------------------------


async def image_node(state: MediaPipelineState) -> Stage:
    """Generate images (parallel with audio/music).

    Params:
        images: $input.images — 图片生成请求列表 [{description, filename, style?, size?}]
    """
    if not state.images:
        return Stage(submit=_noop())


    creator = ImageCreator(output_dir=_assets_dir(state, "images"))

    async def submit():
        bg = await creator.generate_images(state.images)
        return bg

    async def poll(bg_result):
        if hasattr(bg_result, "poll_factory") and bg_result.poll_factory is not None:
            return {"image": await bg_result.poll_factory()}
        return {"image": getattr(bg_result, "result", bg_result)}

    return Stage(submit=submit(), poll=poll)


# ---------------------------------------------------------------------------
# Audio (TTS)
# ---------------------------------------------------------------------------


async def audio_node(state: MediaPipelineState) -> Stage:
    """Generate TTS audio tracks (parallel with image/music).

    Params:
        audios: $input.audios — TTS 音频请求列表 [{text, filename, gender?}]
    """
    if not state.audios:
        return Stage(submit=_noop())


    creator = AudioCreator(output_dir=_assets_dir(state, "audio"))

    async def submit():
        bg = await creator.generate_audios(state.audios)
        return bg

    async def poll(bg_result):
        if hasattr(bg_result, "poll_factory") and bg_result.poll_factory is not None:
            return {"audio": await bg_result.poll_factory()}
        return {"audio": getattr(bg_result, "result", bg_result)}

    return Stage(submit=submit(), poll=poll)


# ---------------------------------------------------------------------------
# Music
# ---------------------------------------------------------------------------


async def music_node(state: MediaPipelineState) -> Stage:
    """Generate music tracks (parallel with image/audio).

    Params:
        musics: $input.musics — 音乐生成请求列表 [{prompt, filename, duration?}]
    """
    if not state.musics:
        return Stage(submit=_noop())


    creator = MusicCreator(output_dir=_assets_dir(state, "music"))

    async def submit():
        bg = await creator.generate_music(state.musics)
        return bg

    async def poll(bg_result):
        if hasattr(bg_result, "poll_factory") and bg_result.poll_factory is not None:
            return {"music": await bg_result.poll_factory()}
        return {"music": getattr(bg_result, "result", bg_result)}

    return Stage(submit=submit(), poll=poll)


# ---------------------------------------------------------------------------
# Video
# ---------------------------------------------------------------------------


def _inject_image_refs(videos: list[dict], image_results) -> list[dict]:
    """Optionally inject image node outputs into video requests.

    If a video request has ``first_frame: "$image:<index>"`` (convention), replace
    it with the corresponding URL from the image node's output. Unresolvable refs
    are left as-is (the video creator will ignore or fail gracefully).
    """
    if not image_results or not isinstance(image_results, (list, dict)):
        return videos

    urls: list[str] = []
    if isinstance(image_results, list):
        for item in image_results:
            if isinstance(item, dict):
                urls.append(item.get("url", item.get("pre_url", "")))
    elif isinstance(image_results, dict):
        for item in image_results.get("results", image_results.get("images", [])):
            if isinstance(item, dict):
                urls.append(item.get("url", item.get("pre_url", "")))

    if not urls:
        return videos

    resolved = []
    for v in videos:
        v = dict(v)  # shallow copy
        for field in ("first_frame", "image"):
            ref = v.get(field, "")
            if isinstance(ref, str) and ref.startswith("$image:"):
                try:
                    idx = int(ref.split(":", 1)[1])
                    if 0 <= idx < len(urls) and urls[idx]:
                        v[field] = urls[idx]
                except (ValueError, IndexError):
                    pass
        resolved.append(v)
    return resolved


async def video_node(state: MediaPipelineState) -> Stage:
    """Generate videos (waits for image node to complete).

    Params:
        videos: $input.videos — 视频生成请求 [{prompt, filename, first_frame?, style?}]
        image: image — 上游 image 节点输出 (注入 first_frame 引用)
    """
    if not state.videos:
        return Stage(submit=_noop())

    # Inject image references if image node produced results
    image_results = getattr(state, "image", None)
    videos = _inject_image_refs(state.videos, image_results)


    creator = VideoCreator(output_dir=_assets_dir(state, "videos"))

    async def submit():
        bg = await creator.generate_videos(videos)
        return bg

    async def poll(bg_result):
        if hasattr(bg_result, "poll_factory") and bg_result.poll_factory is not None:
            return {"video": await bg_result.poll_factory()}
        return {"video": getattr(bg_result, "result", bg_result)}

    return Stage(submit=submit(), poll=poll)


# ---------------------------------------------------------------------------
# Duration measure (audio/music timing)
# ---------------------------------------------------------------------------


async def duration_measure_node(state: MediaPipelineState) -> Stage:
    """Measure audio/music durations for timeline frame calculation.

    Params:
        audio: audio — 上游 audio 节点输出
        music: music — 上游 music 节点输出
    """

    async def submit():
        audio_results = getattr(state, "audio", None)
        music_results = getattr(state, "music", None)

        audio_durations: list[float] = []
        music_durations: list[float] = []

        try:
            from metagpt.utils.workspace_media import probe_media_duration_seconds
        except ImportError:
            # Gracefully degrade — can't measure without the utility
            state.durations = {"audio": audio_durations, "music": music_durations}
            return {"durations": state.durations}

        # Measure audio outputs
        if audio_results and isinstance(audio_results, (list, dict)):
            items = audio_results if isinstance(audio_results, list) else audio_results.get("results", [])
            for item in items:
                if isinstance(item, dict):
                    url = item.get("url", item.get("pre_url", ""))
                    if url and os.path.isfile(url):
                        try:
                            dur = await probe_media_duration_seconds(url)
                            audio_durations.append(dur)
                        except Exception as exc:  # noqa: BLE001
                            logger.debug(f"media_pipeline: audio duration probe failed for {url}: {exc}")

        # Measure music outputs
        if music_results and isinstance(music_results, (list, dict)):
            items = music_results if isinstance(music_results, list) else music_results.get("results", [])
            for item in items:
                if isinstance(item, dict):
                    url = item.get("url", item.get("pre_url", ""))
                    if url and os.path.isfile(url):
                        try:
                            dur = await probe_media_duration_seconds(url)
                            music_durations.append(dur)
                        except Exception as exc:  # noqa: BLE001
                            logger.debug(f"media_pipeline: music duration probe failed for {url}: {exc}")

        state.durations = {"audio": audio_durations, "music": music_durations}
        return {"durations": state.durations}

    return Stage(submit=submit())


# ---------------------------------------------------------------------------
# Render gate (AND-join decision point)
# ---------------------------------------------------------------------------


async def render_gate_node(state: MediaPipelineState) -> Stage:
    """AND-join gate — waits for video + duration_measure, routes to render or done.

    Aggregates the real artifacts produced by the image/audio/music/video tracks
    into a single summary so the terminal notification reflects what was actually
    generated (and what failed), instead of an opaque ``{"gate": "reached"}``.
    """

    def _track(node_output) -> dict:
        """Split a creator node's output into success/failed file lists."""
        results = node_output.get("results", []) if isinstance(node_output, dict) else []
        succeeded = [
            r.get("local_path") or r.get("url") or r.get("filename")
            for r in results
            if isinstance(r, dict) and r.get("status") == "success"
        ]
        failed = [
            {"filename": r.get("filename"), "error": r.get("error")}
            for r in results
            if isinstance(r, dict) and r.get("status") == "failed"
        ]
        return {"succeeded": succeeded, "failed": failed}

    artifacts = {
        "images": _track(getattr(state, "image", None)),
        "audios": _track(getattr(state, "audio", None)),
        "music": _track(getattr(state, "music", None)),
        "videos": _track(getattr(state, "video", None)),
    }
    any_failed = any(track["failed"] for track in artifacts.values())
    will_compose = _can_compose(state)

    summary = {
        "gate": "reached",
        "artifacts": artifacts,
        "has_failures": any_failed,
        "final_video": "pending_compose" if will_compose else "not_composed",
    }
    if not will_compose:
        summary["note"] = (
            "Assets generated but NOT composed into a final video — either no "
            "output directory (promo_dir) was provided or no video clips were "
            "produced. The output is individual clips/images/audio, not a single "
            "finished video."
        )
    return Stage(submit=_noop_result({"render_gate_out": summary}))


# ---------------------------------------------------------------------------
# Final compose (FFmpeg)
# ---------------------------------------------------------------------------


async def promo_node(state: MediaPipelineState) -> Stage:
    """Compose the final video from clips + narration + music via FFmpeg.

    Concats the generated video clips in storyboard order, lays each narration
    track at its clip's start offset, ducks the background music under the
    narration, and trims audio to the video length so timing is unified.

    Params:
        promo: $input.promo — 输出参数 {promo_dir, width, height, source_orientation?}
        video: video — 上游 video 节点输出（视频片段）
        audio: audio — 上游 audio 节点输出（旁白）
        music: music — 上游 music 节点输出（背景音乐）
    """

    async def submit():
        return {"status": "running", "message": "FFmpeg compose started."}

    async def poll(_submit_result):

        promo_dir = state.promo_dir or state.promo.get("promo_dir", "")
        clips = _ordered_local_paths(state.videos, getattr(state, "video", None))
        if not clips:
            return {"promo_out": {"status": "failed", "stage": "preflight",
                    "error": "No local video clips available to compose."}}

        narration = _ordered_local_paths(state.audios, getattr(state, "audio", None))
        music = _ordered_local_paths(
            state.musics, getattr(state, "music", None)
        )

        width = state.promo.get("width", 1920)
        height = state.promo.get("height", 1080)
        output_path = os.path.join(promo_dir, f"promo-{width}x{height}.mp4")

        composer = FfmpegComposer()
        composed = await composer.compose(
            videos=clips,
            narration=narration,
            music=music,
            output_path=output_path,
        )
        return {"promo_out": composed}

    return Stage(submit=submit(), poll=poll)
