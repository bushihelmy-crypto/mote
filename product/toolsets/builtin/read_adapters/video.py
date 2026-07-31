"""Video decomposition and summary adaptation for Read."""

from __future__ import annotations

import os
import tempfile

from mote.runtime.media.video import VideoError, VideoUnavailable, decompose_video


class VideoDecodeUnavailable(Exception):
    """The local video decomposition engine is unavailable."""


class VideoDecodeFailed(Exception):
    """The local video could not be decomposed."""


def _clock(seconds: float) -> str:
    total = int(round(seconds))
    minutes, second = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{second:02d}"
    return f"{minutes:02d}:{second:02d}"


def video_summary(file_path: str, result, *, kept: int) -> str:
    metadata = result.meta
    lines = [f"Read video: {metadata.get('title') or file_path}"]
    duration = metadata.get("duration_seconds") or metadata.get("duration")
    if duration:
        lines.append(f"Duration: {int(float(duration))}s")
    if metadata.get("width") and metadata.get("height"):
        lines.append(f"Resolution: {metadata['width']}x{metadata['height']}")
    lines.append(f"Extracted {kept} frame(s) via the {result.engine} engine; " "shown below in order.")
    lines.extend(f"Note: {note}" for note in result.notes)
    if result.frames[:kept]:
        timestamps = ", ".join(_clock(frame.timestamp) for frame in result.frames[:kept])
        lines.append(f"Frame timestamps: {timestamps}")
    if result.transcript:
        lines.extend(("", "Transcript:", result.transcript))
    return "\n".join(lines)


async def decompose_video_bytes(raw: bytes, extension: str, *, max_frames: int):
    with tempfile.TemporaryDirectory(prefix="mote-video-") as work:
        artifact_path = os.path.join(work, f"artifact.{extension}")
        with open(artifact_path, "wb") as artifact:
            artifact.write(raw)
            artifact.flush()
            os.fsync(artifact.fileno())
        try:
            return await decompose_video(artifact_path, work, max_frames=max_frames)
        except VideoUnavailable as exc:
            raise VideoDecodeUnavailable(str(exc)) from exc
        except VideoError as exc:
            raise VideoDecodeFailed(str(exc)) from exc


__all__ = [
    "VideoDecodeFailed",
    "VideoDecodeUnavailable",
    "decompose_video_bytes",
    "video_summary",
]
