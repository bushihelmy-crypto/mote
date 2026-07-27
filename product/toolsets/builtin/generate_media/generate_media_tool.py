"""``GenerateMedia`` — one-call multimedia generation (image, speech, music, video).

A direct fan-out tool (NOT a graph orchestrator): the model passes explicit
per-asset lists and this generates all four kinds concurrently, blocking until
every asset resolves to its final URL, then returns one compact result. It does
NOT plan assets with a storyboard LLM or auto-compose a final clip — the model
itself decides what to generate. All four list params are native-channel only
(the XML protocol delivers args as strings); omit any kind you don't need.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, ClassVar, Optional

import aiohttp

from mote.contracts.services import ServiceExecutionSemantics
from mote.contracts.tools.effects import ToolEffect
from mote.runtime.errors import ToolNotConfiguredError
from mote.runtime.tools.base_tool import BaseTool
from mote.runtime.tools.capability_types import InvokeService
from mote.runtime.tools.tool_registry import register_tool

# Requested-kind -> (multimodal sub-config attribute, human label, model-field
# names). The tool refuses a kind up-front when its service endpoint/key is
# unconfigured OR its generation model is unset, turning a would-be upstream 4xx
# into a clear ToolNotConfiguredError naming the exact config path
# (multimodal.<attr>). Video carries TWO model fields (text-to-video +
# reference-guided); the rest carry a single ``model``.
_KIND_CONFIG: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "images": ("image_generation", "image", ("model",)),
    "audios": ("audio_generation", "speech/TTS", ("model",)),
    "music": ("music_generation", "music", ("model",)),
    "videos": (
        "video_generation",
        "video",
        ("text_to_video_model", "reference_guided_video_model"),
    ),
}


def _check_configured(multimodal: Any, kinds: list[str]) -> None:
    """Raise ToolNotConfiguredError if any requested *kind* is not usable.

    A media service is usable only once its ``base_url`` and ``api_key`` are
    filled AND its generation model field(s) name a model. An empty endpoint/key
    or an unset model would fail deep inside an HTTP call with an opaque error —
    this front-loads it into an actionable notice pointing at ``multimodal.<attr>``.
    """
    missing: list[str] = []
    for kind in kinds:
        attr, label, model_fields = _KIND_CONFIG[kind]
        cfg = getattr(multimodal, attr)
        if not (cfg.base_url and cfg.api_key):
            missing.append(f"{label} (set multimodal.{attr}.base_url + .api_key)")
            continue
        # Endpoint/key present but no model named → the service can't pick a
        # model to generate with. Surface it the same way (model not configured).
        unset = [f for f in model_fields if not getattr(cfg, f, "")]
        if unset:
            paths = " + ".join(f"multimodal.{attr}.{f}" for f in unset)
            missing.append(f"{label} (no model configured — set {paths})")
    if missing:
        raise ToolNotConfiguredError("Media generation service not configured for: " + "; ".join(missing) + ".")


@register_tool
class GenerateMedia(BaseTool):
    """Generate media assets — images, speech (TTS), music, and video — in one call."""

    name = "GenerateMedia"
    aliases: list[str] = ["generate_media"]
    requires: ClassVar[tuple[str, ...]] = ("invoke_service",)
    effect = ToolEffect.EXTERNAL
    invoke_service: InvokeService
    # Recall synonyms for tool-search: common ways a model asks for media work
    # that the summary line does not spell out.
    keywords: ClassVar[list[str]] = [
        "audio",
        "voice",
        "speech",
        "tts",
        "narration",
        "sound",
        "music",
        "song",
        "soundtrack",
        "image",
        "picture",
        "illustration",
        "video",
        "clip",
        "animation",
        "多媒体",
        "图片",
        "配音",
        "语音",
        "音频",
        "音乐",
        "视频",
        "生成图",
        "生成视频",
    ]
    # Batch generation escapes to remote APIs and may download to disk — a
    # one-shot side effect that must not be blindly replayed, so it stays the
    # conservative EXTERNAL (the default derivation), NOT reconstructable.

    def __init__(
        self,
        multimodal_config: Any,
    ) -> None:
        super().__init__()
        self._multimodal_config = multimodal_config

    def can_resume_started_call(self, call_id: str) -> bool:
        """Re-enter the gateway, which resumes receipts instead of resubmitting."""
        return True

    async def call(
        self,
        *,
        images: Optional[list[dict]] = None,
        audios: Optional[list[dict]] = None,
        music: Optional[list[dict]] = None,
        videos: Optional[list[dict]] = None,
        output_dir: Optional[str] = None,
    ) -> dict:
        """Generate images, speech, music, and/or video assets and wait for the URLs.

        Runs every requested kind concurrently, blocks until all assets finish,
        then returns each asset's URL (and local path if ``output_dir`` is set).
        Omit any kind you don't need. Partial successes are kept; fails only when
        every asset failed.

        Args:
            images: Image specs, each ``{description, filename, size?, image?}``.
                ``image`` is a reference URL/path for image-to-image editing.
            audios: Speech (TTS) specs, each ``{text, filename, gender?, speed?}``.
                ``gender`` is "male"/"female" (voice selection).
            music: Music specs, each ``{prompt, filename, lyrics?, seed?}``.
            videos: Video specs, each ``{prompt, filename, size?, seconds?, image?}``.
                ``image`` (or ``first_frame``) is a reference frame.
            output_dir: Directory to download assets into. Omit to return only
                remote URLs (no local files).
        """
        requested = [
            kind
            for kind, items in (
                ("images", images),
                ("audios", audios),
                ("music", music),
                ("videos", videos),
            )
            if items
        ]
        if not requested:
            return {"message": "No media requested — pass at least one of images/audios/music/videos."}

        _check_configured(self._multimodal_config, requested)

        jobs: list[tuple[str, Any]] = []
        for plural, singular, items in (
            ("images", "image", images),
            ("audios", "audio", audios),
            ("music", "music", music),
            ("videos", "video", videos),
        ):
            if items:
                jobs.append(
                    (
                        plural,
                        self._generate_kind(
                            singular,
                            plural,
                            items,
                            output_dir=output_dir,
                        ),
                    )
                )

        settled = await asyncio.gather(*(coro for _, coro in jobs), return_exceptions=True)

        out: dict[str, Any] = {}
        ok = 0
        for (kind, _), outcome in zip(jobs, settled):
            if isinstance(outcome, BaseException):
                out[kind] = {"error": str(outcome)}
            else:
                out[kind] = _compact(outcome)
                ok += 1
        if ok == 0:
            detail = "; ".join(f"{k}: {v.get('error', 'unknown error')}" for k, v in out.items())
            raise RuntimeError(f"All media generation failed: {detail}")
        return out

    async def _generate_kind(
        self,
        kind: str,
        plural: str,
        items: list[dict],
        *,
        output_dir: str | None,
    ) -> dict[str, Any]:
        async def generate_one(index: int, item: dict) -> dict[str, Any]:
            filename = str(item.get("filename") or _default_filename(kind))
            try:
                value = await self.invoke_service(
                    route_id=f"media.{kind}",
                    capability=f"media.generate.{kind}",
                    operation_key=f"{kind}:{index}",
                    payload={"item": item},
                    semantics=ServiceExecutionSemantics.IDEMPOTENT,
                )
                if not isinstance(value, dict):
                    raise TypeError("media service returned a non-object response")
                result = dict(value)
                if output_dir:
                    try:
                        await _materialize(result, output_dir, filename)
                    except Exception as exc:  # generation is already durable remotely
                        result["materialization_error"] = str(exc)
                return result
            except Exception as exc:  # noqa: BLE001 - keep sibling assets
                return {
                    "status": "failed",
                    "filename": filename,
                    "error": str(exc),
                }

        results = await asyncio.gather(*(generate_one(index, item) for index, item in enumerate(items)))
        successes = [item for item in results if item.get("status") == "success"]
        failed = [item for item in results if item.get("status") != "success"]
        if not successes:
            detail = "; ".join(f"{item.get('filename', '?')}: {item.get('error', 'unknown error')}" for item in failed)
            raise RuntimeError(f"All {plural} failed to generate: {detail}")
        return {
            "summary": f"{len(successes)}/{len(results)} {plural} generated.",
            "results": results,
            "failed": [{"filename": item.get("filename"), "error": item.get("error")} for item in failed],
        }


def _compact(result: dict) -> dict:
    """Reduce a creator's verbose poll dict to just the core per-asset URLs.

    Keeps the one-line ``summary`` and a flat list of ``{filename, url,
    local_path?}`` for successes, plus any ``{filename, error}`` failures — the
    heavy raw poll payload (task ids, request specs) is dropped.
    """
    assets = []
    for r in result.get("results", []):
        if r.get("status") != "success":
            continue
        entry = {
            "filename": r.get("filename"),
            "url": r.get("url") or (r.get("urls") or [""])[0],
        }
        if r.get("local_path"):
            entry["local_path"] = r["local_path"]
        if r.get("materialization_error"):
            entry["materialization_error"] = r["materialization_error"]
        assets.append(entry)
    compact: dict[str, Any] = {"summary": result.get("summary", ""), "assets": assets}
    failed = result.get("failed") or []
    if failed:
        compact["failed"] = failed
    return compact


async def _materialize(result: dict[str, Any], output_dir: str, filename: str) -> None:
    url = result.get("url") or next(iter(result.get("urls") or []), "")
    if not url:
        return
    destination = Path(output_dir).expanduser() / Path(filename).name
    destination.parent.mkdir(parents=True, exist_ok=True)
    timeout = aiohttp.ClientTimeout(total=300)
    async with aiohttp.ClientSession() as session:
        async with session.get(str(url), timeout=timeout) as response:
            response.raise_for_status()
            with destination.open("wb") as stream:
                async for chunk in response.content.iter_chunked(65536):
                    stream.write(chunk)
    result["local_path"] = str(destination)


def _default_filename(kind: str) -> str:
    return {
        "image": "image.png",
        "audio": "audio.mp3",
        "music": "music.wav",
        "video": "video.mp4",
    }[kind]
