"""Shared video-decomposition kernel — turn a LOCAL video into frames + transcript.

The Python-side counterpart of :func:`~mote.runtime.media.html.html_to_markdown`: the
ONE place that decomposes a local video file into the two things a language
model can actually read —

  * a set of *timestamped still frames*, reused VERBATIM as image ``ToolMedia``
    (the framework's existing vision outlet — a frame IS an image, so there is
    zero new media plumbing), and
  * a *timestamped text transcript* (native captions from a sidecar ``.vtt``;
    Whisper / audio transcription is a deliberately deferred v2 seam).

This kernel is LOCAL-ONLY by design: ``Read`` calls it to absorb a local video
file the same way it absorbs an image or a PDF. Networked video is NOT this
kernel's job — the model fetches a URL to a local file first (e.g. bash
``yt-dlp -o clip.mp4 <url>``) and then ``Read``s that local file. This keeps
Read a pure, deterministic, replayable LOCAL perception outlet; fetching lives
in the transport layer (bash / curl / browser).

External tools: ``ffmpeg`` / ``ffprobe`` (probe + frame extraction +
thumbnail-based dedup). Each is PATH-guarded; a missing tool raises
:class:`VideoUnavailable` carrying the install hint, never a bare crash. Pure
decomposition: no LLM, no network, deterministic, single-directional.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

# --- Recognised video containers (used by Read/curl/browser to recognise) ---
VIDEO_EXTENSIONS = frozenset({"mp4", "mkv", "webm", "mov", "m4v", "avi", "flv", "wmv", "mpeg", "mpg", "ts"})

# --- Frame extraction tuning (ported from the reference decomposer) ---------
_MAX_FPS = 2.0
_SCENE_THRESHOLD = 0.20
# Below this many detected shots a clip is effectively static (screen recording,
# talking head) → fall back to uniform sampling rather than trust scene cuts.
_SCENE_MIN_FRAMES = 8
# Below this many decoded keyframes the clip is too sparse for keyframe coverage.
_KEYFRAME_MIN = 4
# Longest edge ffmpeg is allowed to emit (Read-compatible ceiling).
_MAX_READ_DIMENSION = 1998
# Each extracted frame's target width (px); height scales to preserve aspect.
_FRAME_RESOLUTION = 512
# Perceptual dedup: downscale each frame to a grayscale square thumbnail and
# treat two as near-identical when their mean per-pixel diff (0-255) is <= the
# threshold. Conservative — collapses only visually-same shots.
_DEDUP_THUMB = 16
_DEDUP_THRESHOLD = 2.0
# Default frame budget when the caller does not override it.
_DEFAULT_MAX_FRAMES = 60
# Per-subprocess wall-clock ceiling (s). Long enough for a real download/decode,
# bounded so a hung external tool cannot wedge the agent forever.
_DEFAULT_TIMEOUT_S = 600.0

_SHOWINFO_TS_RE = re.compile(r"pts_time:([0-9.]+)")
_VTT_TS_RE = re.compile(r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s+-->\s+(\d{2}):(\d{2}):(\d{2})[.,](\d{3})")
_VTT_TAG_RE = re.compile(r"<[^>]+>")


class VideoError(RuntimeError):
    """A video decomposition failed (bad input, tool error, timeout)."""


class VideoUnavailable(VideoError):
    """A required external tool (ffmpeg / ffprobe) is not installed."""


@dataclass
class VideoFrame:
    """One extracted still frame — reused as an image ``ToolMedia``."""

    timestamp: float  # seconds into the source
    jpeg: bytes  # the JPEG bytes of the frame
    reason: str  # keyframe | scene-change | uniform | first-frame


@dataclass
class VideoResult:
    """The full decomposition: frames + transcript + metadata + human notes."""

    frames: list[VideoFrame] = field(default_factory=list)
    transcript: str = ""  # timestamped text; "" when no captions
    meta: dict = field(default_factory=dict)  # title/duration/dimensions/...
    engine: str = ""  # which frame engine ran (keyframe/scene/uniform)
    notes: list[str] = field(default_factory=list)  # fallbacks / missing captions


# --- Source classification -------------------------------------------------


def is_url(source: str) -> bool:
    """True when *source* is an http(s) URL (vs a local path)."""
    if not source or source.startswith("-"):
        return False
    parsed = urlparse(source)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def looks_like_video_path(path: str) -> bool:
    """True when *path* has a recognised video container extension."""
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return ext in VIDEO_EXTENSIONS


# --- Time parsing / formatting ---------------------------------------------


def parse_time(value: "str | float | int | None") -> "float | None":
    """Parse SS, MM:SS or HH:MM:SS (optional .ms) into seconds; ``None``→None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    parts = s.split(":")
    try:
        if len(parts) == 1:
            return float(parts[0])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        pass
    raise VideoError(f"cannot parse time {value!r} (expected SS, MM:SS, or HH:MM:SS)")


def _stamp(seconds: float) -> str:
    """A ``[MM:SS]`` / ``[H:MM:SS]`` label for a frame or transcript cue."""
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, sec = divmod(rem, 60)
    if hours:
        return f"[{hours}:{minutes:02d}:{sec:02d}]"
    return f"[{minutes:02d}:{sec:02d}]"


# --- Subprocess plumbing (async, PATH-guarded, timeout-bounded) ------------


async def _run(argv: list[str], *, timeout: float) -> tuple[int, bytes, bytes]:
    """Run *argv* to completion, returning ``(rc, stdout, stderr)``.

    Raises :class:`VideoError` on timeout or spawn failure. The binary is
    checked by the caller (which raises the friendlier :class:`VideoUnavailable`
    with an install hint) before we get here.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        raise VideoError(f"failed to run {argv[0]}: {e}")
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise VideoError(f"{argv[0]} timed out after {int(timeout)}s")
    return proc.returncode or 0, stdout, stderr


def _require(tool: str, hint: str) -> None:
    """Raise :class:`VideoUnavailable` when *tool* is not on PATH."""
    if shutil.which(tool) is None:
        raise VideoUnavailable(f"{tool} is not installed. {hint}")


_FFMPEG_HINT = "Install ffmpeg (e.g. `apt install ffmpeg` or `brew install ffmpeg`)."


def _resolve_local(source: str) -> tuple[Path, "Path | None", dict]:
    """Resolve a local *source* path to ``(video_path, subtitle_path, info)``.

    A sidecar ``.vtt`` next to the file supplies native captions. Networked
    sources are out of scope — the caller fetches a URL to a local file first
    (e.g. bash ``yt-dlp``) and passes that local path here.
    """
    p = Path(source).expanduser().resolve()
    if not p.exists():
        raise VideoError(f"file not found: {p}")
    if p.is_dir():
        raise VideoError(f"{p} is a directory, not a video file")
    return p, _pick_subtitle(p.parent, stem=p.stem), {"title": p.name, "path": str(p)}


def _pick_subtitle(out_dir: Path, *, stem: str) -> "Path | None":
    candidates = sorted(out_dir.glob(f"{stem}*.vtt"))
    if not candidates:
        return None
    preferred = [c for c in candidates if any(m in c.name for m in (".en.", ".en-US.", ".en-GB.", ".en-orig."))]
    return preferred[0] if preferred else candidates[0]


async def _probe(video: Path) -> dict:
    """Probe duration / dimensions / audio via ffprobe (JSON)."""
    _require("ffprobe", _FFMPEG_HINT)
    rc, stdout, stderr = await _run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(video),
        ],
        timeout=_DEFAULT_TIMEOUT_S,
    )
    if rc != 0:
        raise VideoError(f"ffprobe failed: {stderr.decode(errors='replace').strip()}")
    data = json.loads(stdout.decode(errors="replace") or "{}")
    streams = data.get("streams", [])
    fmt = data.get("format", {})
    vstream = next((s for s in streams if s.get("codec_type") == "video"), {})
    astream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = float(fmt.get("duration") or vstream.get("duration") or 0)
    return {
        "duration_seconds": duration,
        "width": vstream.get("width"),
        "height": vstream.get("height"),
        "codec": vstream.get("codec_name"),
        "has_audio": astream is not None,
    }


# --- Transcript (native captions only; Whisper is a deferred v2 seam) ------


def _parse_vtt(path: Path) -> list[dict]:
    """Parse a WebVTT file into deduped ``{start,end,text}`` cues."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    lines = text.splitlines()
    cues: list[dict] = []
    i = 0
    while i < len(lines):
        m = _VTT_TS_RE.match(lines[i])
        if not m:
            i += 1
            continue
        start = _vtt_seconds(*m.groups()[:4])
        end = _vtt_seconds(*m.groups()[4:])
        i += 1
        body: list[str] = []
        while i < len(lines) and lines[i].strip():
            cleaned = _VTT_TAG_RE.sub("", lines[i]).strip()
            if cleaned:
                body.append(cleaned)
            i += 1
        joined = " ".join(body).strip()
        if joined:
            cues.append({"start": round(start, 2), "end": round(end, 2), "text": joined})
        i += 1
    return _dedupe_cues(cues)


def _vtt_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def _dedupe_cues(cues: list[dict]) -> list[dict]:
    """Collapse the rolling-duplicate cues typical of YouTube auto-subs."""
    out: list[dict] = []
    for cue in cues:
        if out and cue["text"] == out[-1]["text"]:
            out[-1]["end"] = cue["end"]
            continue
        if out and cue["text"].startswith(out[-1]["text"] + " "):
            out[-1]["text"] = cue["text"]
            out[-1]["end"] = cue["end"]
            continue
        out.append(cue)
    return out


def _format_transcript(cues: list[dict], start: "float | None", end: "float | None") -> str:
    lo = start if start is not None else float("-inf")
    hi = end if end is not None else float("inf")
    lines = [f"{_stamp(c['start'])} {c['text']}" for c in cues if c["end"] >= lo and c["start"] <= hi]
    return "\n".join(lines)


# --- Frame extraction ------------------------------------------------------


def _scale_filter() -> str:
    return (
        f"scale=w='min({_FRAME_RESOLUTION},iw)':h='min({_MAX_READ_DIMENSION},ih)':"
        "force_original_aspect_ratio=decrease:force_divisible_by=2"
    )


def _auto_fps(duration: float, max_frames: int) -> tuple[float, int]:
    """Pick an fps targeting a sensible frame budget for the (sub)clip."""
    if duration <= 0:
        return 1.0, 1
    if duration <= 30:
        target = min(max_frames, max(12, int(round(duration))))
    elif duration <= 60:
        target = min(max_frames, 40)
    elif duration <= 180:
        target = min(max_frames, 60)
    elif duration <= 600:
        target = min(max_frames, 80)
    else:
        target = max_frames
    fps = min(_MAX_FPS, target / duration)
    target = min(max_frames, max(1, int(round(fps * duration))))
    return fps, target


def _even_indices(count: int, n: int) -> list[int]:
    """Indices of ``n`` evenly-spaced items out of ``count`` (first+last kept)."""
    if n >= count:
        return list(range(count))
    if n <= 1:
        return [0]
    return [round(i * (count - 1) / (n - 1)) for i in range(n)]


async def _ffmpeg_frames(
    video: Path,
    work: Path,
    *,
    vf: str,
    start: "float | None",
    end: "float | None",
    max_frames: "int | None",
    keyframe_only: bool = False,
    vsync_vfr: bool = False,
) -> tuple[list[Path], list[float]]:
    """Run one ffmpeg extraction pass; return (frame_paths, showinfo_timestamps).

    ``vf`` is the video filter (already includes ``showinfo`` when timestamps
    are wanted). Frames are written as ``frame_%04d.jpg`` under *work*.
    """
    _require("ffmpeg", _FFMPEG_HINT)
    work.mkdir(parents=True, exist_ok=True)
    for stale in work.glob("frame_*.jpg"):
        stale.unlink()

    argv = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "info" if "showinfo" in vf else "error",
        "-y",
    ]
    if start is not None:
        argv += ["-ss", f"{start:.3f}"]
    if end is not None:
        argv += ["-to", f"{end:.3f}"]
    if keyframe_only:
        argv += ["-skip_frame", "nokey"]
    argv += ["-i", str(video), "-vf", vf]
    if vsync_vfr:
        argv += ["-vsync", "vfr"]
    if max_frames is not None:
        argv += ["-frames:v", str(max_frames)]
    argv += ["-q:v", "4", str(work / "frame_%04d.jpg")]

    rc, _out, stderr = await _run(argv, timeout=_DEFAULT_TIMEOUT_S)
    if rc != 0:
        raise VideoError(f"ffmpeg frame extraction failed: {stderr.decode(errors='replace').strip()}")
    err = stderr.decode(errors="replace")
    timestamps = [float(m.group(1)) for m in _SHOWINFO_TS_RE.finditer(err)]
    return sorted(work.glob("frame_*.jpg")), timestamps


async def _thumbs(paths: list[Path]) -> list[bytes]:
    """Decode every frame to a grayscale square thumbnail in one ffmpeg pass.

    Fail-open: any error / count mismatch returns ``[]`` so the caller skips
    dedup rather than breaking extraction.
    """
    if not paths:
        return []
    m = re.match(r"(.*?)(\d+)(\.[A-Za-z0-9]+)$", paths[0].name)
    if m is None:
        return []
    prefix, digits, ext = m.group(1), m.group(2), m.group(3)
    pattern = str(paths[0].parent / f"{prefix}%0{len(digits)}d{ext}")
    argv = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-start_number",
        str(int(digits)),
        "-i",
        pattern,
        "-vf",
        f"scale={_DEDUP_THUMB}:{_DEDUP_THUMB},format=gray",
        "-f",
        "rawvideo",
        "-",
    ]
    rc, stdout, _err = await _run(argv, timeout=_DEFAULT_TIMEOUT_S)
    if rc != 0:
        return []
    chunk = _DEDUP_THUMB * _DEDUP_THUMB
    if len(stdout) != chunk * len(paths):
        return []
    return [stdout[i * chunk : (i + 1) * chunk] for i in range(len(paths))]


def _frame_delta(a: bytes, b: bytes) -> float:
    if not a or len(a) != len(b):
        return float("inf")
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def _dedupe(paths: list[Path], thumbs: list[bytes]) -> tuple[list[int], int]:
    """Greedily drop frames within the delta threshold of the last kept one.

    Returns ``(kept_indices, dropped_count)``; a no-op when thumbnails are
    unavailable or there is at most one frame.
    """
    if len(thumbs) != len(paths) or len(paths) <= 1:
        return list(range(len(paths))), 0
    kept = [0]
    last = thumbs[0]
    dropped = 0
    for i in range(1, len(paths)):
        if _frame_delta(thumbs[i], last) <= _DEDUP_THRESHOLD:
            dropped += 1
        else:
            kept.append(i)
            last = thumbs[i]
    return kept, dropped


async def _select_frames(
    video: Path,
    work: Path,
    *,
    duration: float,
    max_frames: int,
    start: "float | None",
    end: "float | None",
) -> tuple[list[VideoFrame], str, list[str]]:
    """Pick representative frames: keyframe → scene → uniform, then dedup+cap.

    Returns ``(frames, engine, notes)``. Every tier detects candidates across
    the whole (sub)range, drops near-duplicates, then even-samples down to
    ``max_frames`` (first+last always kept) so coverage spans the clip.
    """
    notes: list[str] = []
    offset = start or 0.0
    eff_end = end if end is not None else duration
    eff_duration = max(0.0, eff_end - offset)

    # Tier 1 — keyframes (cheap, near-instant; encoders cut at scene changes).
    paths, timestamps = await _ffmpeg_frames(
        video,
        work,
        vf=f"{_scale_filter()},showinfo",
        start=start,
        end=end,
        max_frames=None,
        keyframe_only=True,
        vsync_vfr=True,
    )
    engine = "keyframe"
    if len(paths) < _KEYFRAME_MIN:
        # Too sparse for keyframe coverage — fall through to uniform.
        notes.append("few keyframes; sampled uniformly")
        fps, _ = _auto_fps(eff_duration, max_frames)
        paths, timestamps = await _ffmpeg_frames(
            video,
            work,
            vf=f"fps={fps},{_scale_filter()}",
            start=start,
            end=end,
            max_frames=max_frames,
        )
        engine = "uniform"
        # Uniform timestamps derive from the fps grid (no showinfo).
        timestamps = [round(offset + (i / fps if fps > 0 else 0.0), 2) for i in range(len(paths))]

    if not paths:
        return [], engine, notes

    thumbs = await _thumbs(paths)
    kept_idx, dropped = _dedupe(paths, thumbs)
    if dropped:
        notes.append(f"dropped {dropped} near-duplicate frame(s)")
    kept_paths = [paths[i] for i in kept_idx]
    kept_ts = [timestamps[i] if i < len(timestamps) else offset for i in kept_idx]

    # Even-sample down to the budget (first + last always kept).
    sel = _even_indices(len(kept_paths), max_frames)
    frames: list[VideoFrame] = []
    for out_i, src_i in enumerate(sel):
        p = kept_paths[src_i]
        try:
            jpeg = p.read_bytes()
        except OSError:
            continue
        ts = round(offset + kept_ts[src_i], 2) if engine == "keyframe" else round(kept_ts[src_i], 2)
        reason = "first-frame" if out_i == 0 else engine
        frames.append(VideoFrame(timestamp=ts, jpeg=jpeg, reason=reason))
    return frames, engine, notes


# --- Public entry point ----------------------------------------------------


async def decompose_video(
    source: str,
    work_dir: str,
    *,
    start: "str | float | None" = None,
    end: "str | float | None" = None,
    max_frames: int = _DEFAULT_MAX_FRAMES,
) -> VideoResult:
    """Decompose a LOCAL video file into frames + a transcript.

    This is the ONE video-understanding kernel: it resolves the local file,
    probes it, extracts native captions from a sidecar ``.vtt`` when present,
    and picks a representative set of timestamped frames bounded by
    ``max_frames``. The frames are returned as raw JPEG bytes for the caller to
    wrap as image ``ToolMedia`` — the framework's existing vision outlet.
    Whisper / audio transcription is intentionally NOT wired here (a deferred v2
    seam); a video with no captions returns frames plus a note.

    Networked video is out of scope: fetch a URL to a local file first (e.g.
    bash ``yt-dlp -o clip.mp4 <url>``) and pass that local path here.

    Args:
        source: A local video file path.
        work_dir: A scratch directory for extracted frames (the caller owns its
            lifecycle — typically a per-call temp dir).
        start: Optional focus-window start (SS / MM:SS / HH:MM:SS or seconds).
        end: Optional focus-window end.
        max_frames: Hard cap on returned frames (budget for token cost).

    Raises:
        VideoUnavailable: a required external tool (ffmpeg/ffprobe) is missing.
        VideoError: bad input, or an ffmpeg/ffprobe failure.
    """
    work = Path(work_dir)
    start_s = parse_time(start)
    end_s = parse_time(end)

    video, subtitle, info = _resolve_local(source)
    probe = await _probe(video)
    meta = {**info, **probe}
    duration = probe.get("duration_seconds") or 0.0

    frames, engine, notes = await _select_frames(
        video,
        work,
        duration=duration,
        max_frames=max(1, max_frames),
        start=start_s,
        end=end_s,
    )

    transcript = ""
    if subtitle is not None:
        transcript = _format_transcript(_parse_vtt(subtitle), start_s, end_s)
    if not transcript:
        notes.append("no captions available — frames only " "(audio transcription/Whisper is not yet wired)")

    return VideoResult(frames=frames, transcript=transcript, meta=meta, engine=engine, notes=notes)


__all__ = [
    "VIDEO_EXTENSIONS",
    "VideoError",
    "VideoFrame",
    "VideoResult",
    "VideoUnavailable",
    "decompose_video",
    "is_url",
    "looks_like_video_path",
    "parse_time",
]
