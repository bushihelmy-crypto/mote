"""Lightweight async-task media creators (image, audio TTS, music, video) plus a
local FFmpeg composer for the final video.

The generators are thin wrappers around the platform's async task API:
  POST submit → GET poll → collect URLs.

They replace the old run_rollout creators with zero legacy deps.
Each `generate_*` method returns a ``BgTaskResult`` compatible with the bggraph
Stage(submit, poll) contract used by media_pipeline/nodes.py.

``FfmpegComposer`` stitches the generated clips, narration and background music
into one finished video locally via FFmpeg — replacing the previous Remotion
renderer (no Node/pnpm/Remotion project required).
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import shutil
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse

import aiohttp

from metagpt.common.config.meta_config import Config
from metagpt.common.exception import RecoveryAction, RecoveryRunner
from metagpt.common.exception.media import (
    MediaGenerationError,
    PermanentMediaGenerationError,
    classify_media_failure,
)
from metagpt.common.logs import logger
from metagpt.executor.tasks.types import BgTaskResult


# ---------------------------------------------------------------------------
# Shared async-task polling
# ---------------------------------------------------------------------------

_POLL_INTERVAL = 3.0  # seconds
_POLL_TIMEOUT = 600.0  # seconds

# Per-resource retry policy. Each individual media asset (one image / audio /
# music / video) that fails on a transient error is retried on its own — the
# whole batch is NOT re-submitted. Mirrors the bggraph engine's node-level
# policy (3 attempts, exponential backoff with full jitter) but scoped to a
# single resource so a flaky asset never forces healthy siblings to regenerate.
_ITEM_RETRIES = 3  # max retries per resource for transient failures
_ITEM_RETRY_WAIT = 1.0  # base backoff seconds (grows exponentially per attempt)
_ITEM_MAX_BACKOFF = 30.0  # cap for a single retry sleep


def _item_retry_delay(attempt: int) -> float:
    """Exponential backoff with full jitter for retry *attempt* (1-based)."""
    ceiling = min(_ITEM_RETRY_WAIT * (2 ** (attempt - 1)), _ITEM_MAX_BACKOFF)
    return random.uniform(0, ceiling)


async def _generate_one_with_retry(
    attempt_fn: Callable[[], Awaitable[dict]],
    *,
    filename: str,
    noun: str,
) -> dict:
    """Run a single-resource generation under per-item retry.

    ``attempt_fn`` is a no-arg coroutine factory that performs ONE full
    submit→poll cycle for a single asset and returns its success dict. It must
    raise a typed media error (``MediaGenerationError`` /
    ``PermanentMediaGenerationError`` via :func:`classify_media_failure`) or a
    transient SDK/network error on failure.

    Retry semantics are delegated to the shared :class:`RecoveryRunner` so the
    media tier reuses the codebase's single retry judgement: a typed error's
    ``recovery`` hint (retryable → RETRY, permanent → ABORT/fail-fast) or, for
    an untyped error, ``is_retryable``. Only transient failures consume the
    budget; a permanent failure (auth / quota / content moderation / malformed
    request) aborts immediately without burning retries. When the budget is
    exhausted (or the failure is permanent), the per-item failure record is
    returned — the batch keeps the partial result instead of aborting.
    """
    attempts = 0

    async def _call() -> dict:
        nonlocal attempts
        attempts += 1
        return await attempt_fn()

    async def _retry(exc: BaseException) -> bool:
        logger.warning(
            f"{noun} '{filename}' failed (attempt {attempts}/"
            f"{_ITEM_RETRIES + 1}): {exc}; retrying."
        )
        await asyncio.sleep(_item_retry_delay(attempts))
        return True

    runner = RecoveryRunner({RecoveryAction.RETRY: _retry}, max_recoveries=_ITEM_RETRIES)
    try:
        return await runner.run(_call)
    except Exception as e:  # noqa: BLE001 — give up: record the per-item failure
        logger.error(f"{noun} '{filename}' permanently failed after {attempts} attempt(s): {e}")
        return _failure_entry(filename, e)


async def _get_json(
    session: aiohttp.ClientSession, url: str, headers: dict
) -> dict:
    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as resp:
        resp.raise_for_status()
        return await resp.json()


async def _post_json(
    session: aiohttp.ClientSession, url: str, headers: dict, payload: dict
) -> dict:
    async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=300)) as resp:
        resp.raise_for_status()
        return await resp.json()


async def _poll_until_done(
    session: aiohttp.ClientSession,
    base_url: str,
    headers: dict,
    task_id: str,
    *,
    interval: float = _POLL_INTERVAL,
    timeout: float = _POLL_TIMEOUT,
) -> dict:
    """Poll GET /v1/async-tasks/{task_id} until completed or failed."""
    elapsed = 0.0
    while True:
        data = await _get_json(session, f"{base_url}/async-tasks/{task_id}", headers)
        status = data.get("status", "")
        if status == "completed":
            return data
        if status == "failed":
            err = data.get("error")
            if isinstance(err, dict):
                msg = err.get("message", "") or str(err)
                code = str(err.get("code") or err.get("type") or data.get("code") or "")
            else:
                msg = str(err or "failed")
                code = str(data.get("code") or "")
            raise classify_media_failure(msg, task_id=task_id, code=code)
        if elapsed >= timeout:
            raise TimeoutError(f"Task {task_id} timed out after {timeout}s")
        await asyncio.sleep(interval)
        elapsed += interval


def _extract_urls(data: dict) -> list[str]:
    """Extract result URLs from a completed task response."""
    urls = data.get("urls") or []
    if not urls and data.get("url"):
        urls = [data["url"]]
    if not urls:
        urls = data.get("pre_urls") or []
    return [u for u in urls if u]


async def _download_to(
    session: aiohttp.ClientSession,
    url: str,
    dest: Path,
    *,
    headers: Optional[dict] = None,
) -> Path:
    """Download url to dest file. Returns dest path."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=300)) as resp:
        resp.raise_for_status()
        with open(dest, "wb") as f:
            async for chunk in resp.content.iter_chunked(65536):
                f.write(chunk)
    return dest


async def _download_results(
    session: aiohttp.ClientSession,
    results: list[dict],
    output_dir: Path,
) -> list[dict]:
    """Download URLs in results to output_dir, adding 'local_path' to each entry."""
    for item in results:
        if item.get("status") != "success":
            continue
        urls = item.get("urls") or ([item["url"]] if item.get("url") else [])
        if not urls:
            continue
        url = urls[0]
        filename = item.get("filename") or Path(urlparse(url).path).name or "file"
        dest = output_dir / filename
        try:
            await _download_to(session, url, dest)
            item["local_path"] = str(dest)
        except Exception as e:
            logger.warning(f"Download failed for {filename}: {e}")
    return results


def _summarize_poll_results(results: list[dict], noun: str) -> dict:
    """Build the poll summary, raising if every item failed.

    A node that produced zero artifacts must fail loudly rather than report
    success with an empty result — otherwise an all-failed batch is silently
    swallowed and the task is wrongly marked SUCCESS. Partial failures are kept
    in the returned dict (``failed`` list) so downstream nodes / notifications
    can surface them without aborting the whole pipeline.

    When every item failed, the raised error's retryability mirrors the policy
    "retry whatever can be tried": the batch is **retryable** (so the bggraph
    engine re-submits) unless *every* failure was classified permanent (auth /
    quota / malformed request / content moderation), in which case re-submitting
    is pointless and we fail fast.
    """
    success = [r for r in results if r.get("status") == "success"]
    failed = [r for r in results if r.get("status") == "failed"]
    if results and not success:
        detail = "; ".join(
            f"{r.get('filename', '?')}: {r.get('error', 'unknown error')}" for r in failed
        )
        # Permanent only if EVERY failure is permanent — a single transient
        # failure makes the whole batch worth re-submitting.
        all_permanent = bool(failed) and all(r.get("permanent") for r in failed)
        exc_cls = PermanentMediaGenerationError if all_permanent else MediaGenerationError
        raise exc_cls(f"All {len(results)} {noun} failed to generate: {detail}")
    return {
        "summary": f"{len(success)}/{len(results)} {noun} generated.",
        "results": results,
        "failed": [{"filename": r.get("filename"), "error": r.get("error")} for r in failed],
    }


def _failure_entry(filename: str, exc: BaseException) -> dict:
    """Build a per-item failure record, tagging whether the error is permanent.

    The ``permanent`` flag is what :func:`_summarize_poll_results` uses to decide
    the batch-level retryability. A :class:`PermanentMediaGenerationError` is
    permanent; everything else (transient :class:`MediaGenerationError`, network
    blips, timeouts) is retryable.
    """
    permanent = isinstance(exc, MediaGenerationError) and not exc.retryable
    return {"status": "failed", "filename": filename, "error": str(exc), "permanent": permanent}


# ---------------------------------------------------------------------------
# AudioCreator (TTS)
# ---------------------------------------------------------------------------


class AudioCreator:
    """Async TTS audio generation via POST /v1/audio/speech/async."""

    # Voice mapping: (model, gender) -> voice name
    VOICE_MAP: dict[tuple[str, str], str] = {
        ("qwen3-tts-flash", "male"): "echo",
        ("qwen3-tts-flash", "female"): "alloy",
        ("gemini-2.5-pro-preview-tts", "male"): "echo",
        ("gemini-2.5-pro-preview-tts", "female"): "alloy",
        ("eleven_v3", "male"): "echo",
        ("eleven_v3", "female"): "alloy",
        ("gpt-4o-mini-tts", "male"): "echo",
        ("gpt-4o-mini-tts", "female"): "nova",
    }

    def __init__(self, output_dir: Optional[str] = None) -> None:
        cfg = Config.default().multimodal.audio_generation
        self._api_key: str = cfg.api_key
        self._base_url: str = cfg.base_url.rstrip("/")
        self._model: str = cfg.model
        self._output_dir: Optional[Path] = Path(output_dir) if output_dir else None

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    def _voice(self, model: str, gender: str) -> str:
        return self.VOICE_MAP.get((model, gender)) or ("echo" if gender == "male" else "alloy")

    async def generate_audios(self, audios: list[dict], **_: Any) -> BgTaskResult:
        """Submit TTS tasks and poll in background.

        Each item: {text, filename, gender?, model?, speed?}
        Returns BgTaskResult with pre_urls immediately; poll resolves final URLs.
        """
        if not audios:
            return BgTaskResult.foreground({"results": [], "message": "No audios to generate."})

        headers = self._headers()
        submitted: list[dict] = []

        async with aiohttp.ClientSession() as session:
            for item in audios:
                text = item.get("text") or item.get("input", "")
                filename = item.get("filename", "audio.mp3")
                model = self._model
                gender = item.get("gender", "female")
                speed = item.get("speed", 1.0)

                payload = {
                    "model": model,
                    "input": text,
                    "voice": self._voice(model, gender),
                    "speed": speed,
                    "response_format": "mp3",
                    "filename": filename,
                }
                try:
                    data = await _post_json(session, f"{self._base_url}/audio/speech/async", headers, payload)
                    task_id = data.get("id") or data.get("task_id") or ""
                    if not task_id:
                        raise RuntimeError("API returned empty task_id")
                    submitted.append({
                        "task_id": task_id,
                        "filename": filename,
                        "payload": payload,
                        "pre_urls": data.get("pre_urls") or [],
                        "status": data.get("status", "queued"),
                    })
                except Exception as e:
                    logger.error(f"Audio submit failed for '{filename}': {e}")
                    submitted.append({
                        "filename": filename,
                        "payload": payload,
                        "status": "failed",
                        "error": str(e),
                    })

        immediate = {
            "submitted": [s for s in submitted if s.get("task_id")],
            "failed": [s for s in submitted if s.get("status") == "failed"],
            "message": f"{len([s for s in submitted if s.get('task_id')])} audio tasks submitted.",
        }

        # Resources that failed to even submit are retried (re-submitted) in the
        # poll phase too — a transient submit error shouldn't drop the asset.
        pending = [s for s in submitted if s.get("task_id") or s.get("payload")]
        if not pending:
            return BgTaskResult.foreground(immediate)

        async def _poll() -> str:
            return await self._poll_all(pending)

        return BgTaskResult.hybrid(result=immediate, poll_factory=_poll, command_name="generate audios")

    async def _submit_one(self, session, payload: dict) -> str:
        """Submit one TTS task, returning its task_id (raises on failure)."""
        data = await _post_json(session, f"{self._base_url}/audio/speech/async", self._headers(), payload)
        task_id = data.get("id") or data.get("task_id") or ""
        if not task_id:
            raise RuntimeError("API returned empty task_id")
        return task_id

    async def _resolve_one(self, session, item: dict) -> dict:
        """Resolve a single audio asset: poll its task (re-submit if needed).

        Used as the per-item retry attempt. On the first call the upfront
        ``task_id`` is reused; on a retry (after a transient failure) the slot's
        task_id is cleared so a fresh task is submitted before polling — so the
        retry regenerates ONLY this one resource.
        """
        filename = item["filename"]
        task_id = item.get("task_id")
        if not task_id:
            task_id = await self._submit_one(session, item["payload"])
            item["task_id"] = task_id
        try:
            data = await _poll_until_done(session, self._base_url, self._headers(), task_id)
        except Exception:
            # Force a fresh submit on the next retry attempt.
            item["task_id"] = None
            raise
        urls = _extract_urls(data)
        return {"status": "success", "filename": filename, "urls": urls, "url": urls[0] if urls else ""}

    async def _poll_all(self, pending: list[dict]) -> dict:
        results: list[dict] = []
        async with aiohttp.ClientSession() as session:
            for item in pending:
                result = await _generate_one_with_retry(
                    lambda it=item: self._resolve_one(session, it),
                    filename=item["filename"],
                    noun="Audio",
                )
                results.append(result)
            if self._output_dir:
                await _download_results(session, results, self._output_dir)
        return _summarize_poll_results(results, "audios")


# ---------------------------------------------------------------------------
# MusicCreator
# ---------------------------------------------------------------------------


class MusicCreator:
    """Async music generation via POST /v1/audio/music/async."""

    def __init__(self, output_dir: Optional[str] = None) -> None:
        cfg = Config.default().multimodal.music_generation
        self._api_key: str = cfg.api_key
        self._base_url: str = cfg.base_url.rstrip("/")
        self._model: str = cfg.model
        self._response_format: str = cfg.response_format or "url"
        self._output_dir: Optional[Path] = Path(output_dir) if output_dir else None

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    async def generate_music(self, tracks: list[dict], **_: Any) -> BgTaskResult:
        """Submit music generation tasks and poll in background.

        Each item: {prompt, filename, model?, n?, negative_prompt?, seed?, lyrics?, ...}
        Returns BgTaskResult with pre_urls immediately; poll resolves final URLs.
        """
        if not tracks:
            return BgTaskResult.foreground({"results": [], "message": "No music to generate."})

        headers = self._headers()
        submitted: list[dict] = []

        async with aiohttp.ClientSession() as session:
            for item in tracks:
                prompt = item.get("prompt", "")
                filename = item.get("filename", "music.wav")
                model = self._model

                payload: dict[str, Any] = {
                    "model": model,
                    "prompt": prompt,
                    "n": item.get("n", 1),
                    "response_format": item.get("response_format") or self._response_format,
                    "filename": filename,
                }
                # Optional fields
                for key in ("negative_prompt", "seed", "lyrics",
                            "audio_format", "sample_rate", "bitrate", "voice_id"):
                    val = item.get(key)
                    if val is not None:
                        payload[key] = val

                try:
                    data = await _post_json(session, f"{self._base_url}/audio/music/async", headers, payload)
                    task_id = data.get("id") or data.get("task_id") or ""
                    if not task_id:
                        raise RuntimeError("API returned empty task_id")
                    submitted.append({
                        "task_id": task_id,
                        "filename": filename,
                        "payload": payload,
                        "pre_urls": data.get("pre_urls") or [],
                        "status": data.get("status", "queued"),
                    })
                except Exception as e:
                    logger.error(f"Music submit failed for '{filename}': {e}")
                    submitted.append({
                        "filename": filename,
                        "payload": payload,
                        "status": "failed",
                        "error": str(e),
                    })

        immediate = {
            "submitted": [s for s in submitted if s.get("task_id")],
            "failed": [s for s in submitted if s.get("status") == "failed"],
            "message": f"{len([s for s in submitted if s.get('task_id')])} music tasks submitted.",
        }

        pending = [s for s in submitted if s.get("task_id") or s.get("payload")]
        if not pending:
            return BgTaskResult.foreground(immediate)

        async def _poll() -> str:
            return await self._poll_all(pending)

        return BgTaskResult.hybrid(result=immediate, poll_factory=_poll, command_name="generate music")

    async def _submit_one(self, session, payload: dict) -> str:
        """Submit one music task, returning its task_id (raises on failure)."""
        data = await _post_json(session, f"{self._base_url}/audio/music/async", self._headers(), payload)
        task_id = data.get("id") or data.get("task_id") or ""
        if not task_id:
            raise RuntimeError("API returned empty task_id")
        return task_id

    async def _resolve_one(self, session, item: dict) -> dict:
        """Resolve a single music asset: poll its task (re-submit if needed)."""
        filename = item["filename"]
        task_id = item.get("task_id")
        if not task_id:
            task_id = await self._submit_one(session, item["payload"])
            item["task_id"] = task_id
        try:
            data = await _poll_until_done(session, self._base_url, self._headers(), task_id)
        except Exception:
            item["task_id"] = None
            raise
        urls = _extract_urls(data)
        return {"status": "success", "filename": filename, "urls": urls, "url": urls[0] if urls else ""}

    async def _poll_all(self, pending: list[dict]) -> dict:
        results: list[dict] = []
        async with aiohttp.ClientSession() as session:
            for item in pending:
                result = await _generate_one_with_retry(
                    lambda it=item: self._resolve_one(session, it),
                    filename=item["filename"],
                    noun="Music",
                )
                results.append(result)
            if self._output_dir:
                await _download_results(session, results, self._output_dir)
        return _summarize_poll_results(results, "music tracks")


# ---------------------------------------------------------------------------
# ImageCreator
# ---------------------------------------------------------------------------


class ImageCreator:
    """Async image generation via POST /v1/images/generations/async.

    Supports text-to-image (JSON) and image-to-image editing (multipart).
    """

    def __init__(self, output_dir: Optional[str] = None) -> None:
        cfg = Config.default().multimodal.image_generation
        self._api_key: str = cfg.api_key
        self._base_url: str = cfg.base_url.rstrip("/")
        self._model: str = cfg.model
        self._output_dir: Optional[Path] = Path(output_dir) if output_dir else None

    def _headers(self, content_type: str = "application/json") -> dict:
        h: dict[str, str] = {"Authorization": f"Bearer {self._api_key}"}
        if content_type:
            h["Content-Type"] = content_type
        return h

    async def generate_images(self, images: list[dict], **_: Any) -> BgTaskResult:
        """Submit image generation tasks and poll in background.

        Each item: {description, filename, style?, size?, image?(ref for i2i)}
        """
        if not images:
            return BgTaskResult.foreground({"results": [], "message": "No images to generate."})

        submitted: list[dict] = []

        async with aiohttp.ClientSession() as session:
            for i, item in enumerate(images):
                filename = item.get("filename", "image.png")
                description = item.get("description") or item.get("prompt", "")
                size = item.get("size", "1024x1024")
                ref_image = item.get("image")
                spec = {
                    "filename": filename,
                    "description": description,
                    "size": size,
                    "ref_image": ref_image,
                }

                try:
                    data = await self._submit_spec(session, spec)
                    task_id = data.get("id") or data.get("task_id") or ""
                    if not task_id:
                        raise RuntimeError("API returned empty task_id")
                    submitted.append({
                        "task_id": task_id,
                        "filename": filename,
                        "spec": spec,
                        "pre_urls": data.get("pre_urls") or [],
                        "status": data.get("status", "queued"),
                    })
                except Exception as e:
                    logger.error(f"Image submit failed for '{filename}': {e}")
                    submitted.append({
                        "filename": filename,
                        "spec": spec,
                        "status": "failed",
                        "error": str(e),
                    })
                if i < len(images) - 1:
                    await asyncio.sleep(0.5)

        immediate = {
            "submitted": [s for s in submitted if s.get("task_id")],
            "failed": [s for s in submitted if s.get("status") == "failed"],
            "message": f"{len([s for s in submitted if s.get('task_id')])} image tasks submitted.",
        }

        pending = [s for s in submitted if s.get("task_id") or s.get("spec")]
        if not pending:
            return BgTaskResult.foreground(immediate)

        async def _poll() -> str:
            return await self._poll_all(pending)

        return BgTaskResult.hybrid(result=immediate, poll_factory=_poll, command_name="generate images")

    async def _submit_spec(self, session, spec: dict) -> dict:
        """Submit one image task (gen or edit) from a stored spec."""
        if spec.get("ref_image"):
            return await self._submit_edit(
                session, spec["description"], spec["filename"], spec["size"], spec["ref_image"]
            )
        return await self._submit_gen(
            session, self._headers(), spec["description"], spec["filename"], spec["size"]
        )

    async def _submit_gen(self, session, headers, description, filename, size) -> dict:
        payload = {
            "model": self._model,
            "prompt": description,
            "n": 1,
            "size": size,
            "filename": filename,
        }
        return await _post_json(session, f"{self._base_url}/images/generations/async", headers, payload)

    async def _submit_edit(self, session, description, filename, size, ref_image) -> dict:
        """Submit image-to-image edit (multipart/form-data)."""
        headers = {"Authorization": f"Bearer {self._api_key}"}
        form = aiohttp.FormData()
        form.add_field("model", self._model)
        form.add_field("prompt", description)
        form.add_field("n", "1")
        form.add_field("size", size)
        form.add_field("filename", filename)
        # ref_image is a URL or local path — pass as image field
        if ref_image.startswith(("http://", "https://")):
            # Download and attach
            async with session.get(ref_image) as resp:
                img_bytes = await resp.read()
            form.add_field("image", img_bytes, filename="ref.png", content_type="image/png")
        else:
            import os
            with open(ref_image, "rb") as f:
                img_bytes = f.read()
            form.add_field("image", img_bytes, filename=os.path.basename(ref_image), content_type="image/png")
        async with session.post(
            f"{self._base_url}/images/edits/async", headers=headers, data=form,
            timeout=aiohttp.ClientTimeout(total=300),
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _resolve_one(self, session, item: dict) -> dict:
        """Resolve a single image asset: poll its task (re-submit if needed)."""
        filename = item["filename"]
        task_id = item.get("task_id")
        if not task_id:
            data = await self._submit_spec(session, item["spec"])
            task_id = data.get("id") or data.get("task_id") or ""
            if not task_id:
                raise RuntimeError("API returned empty task_id")
            item["task_id"] = task_id
        try:
            data = await _poll_until_done(session, self._base_url, self._headers(), task_id)
        except Exception:
            item["task_id"] = None
            raise
        urls = _extract_urls(data)
        return {"status": "success", "filename": filename, "urls": urls, "url": urls[0] if urls else ""}

    async def _poll_all(self, pending: list[dict]) -> dict:
        results: list[dict] = []
        async with aiohttp.ClientSession() as session:
            for item in pending:
                result = await _generate_one_with_retry(
                    lambda it=item: self._resolve_one(session, it),
                    filename=item["filename"],
                    noun="Image",
                )
                results.append(result)
            if self._output_dir:
                await _download_results(session, results, self._output_dir)
        return _summarize_poll_results(results, "images")


# ---------------------------------------------------------------------------
# VideoCreator
# ---------------------------------------------------------------------------


class VideoCreator:
    """Async video generation via POST /v1/videos (OpenAI-compatible endpoint).

    Uses the OpenAI SDK's `client.videos.create` / `client.videos.retrieve`.
    """

    def __init__(self, output_dir: Optional[str] = None) -> None:
        cfg = Config.default().multimodal.video_generation
        self._api_key: str = cfg.api_key
        self._base_url: str = cfg.base_url.rstrip("/")
        self._t2v_model: str = cfg.text_to_video_model
        self._i2v_model: str = cfg.reference_guided_video_model
        self._output_dir: Optional[Path] = Path(output_dir) if output_dir else None

    def _client(self):
        from openai import AsyncOpenAI
        return AsyncOpenAI(api_key=self._api_key, base_url=self._base_url)

    async def generate_videos(self, videos: list[dict], **_: Any) -> BgTaskResult:
        """Submit video generation tasks and poll in background.

        Each item: {prompt, filename, model?, size?, seconds?, image?, first_frame?}
        """
        if not videos:
            return BgTaskResult.foreground({"results": [], "message": "No videos to generate."})

        client = self._client()
        submitted: list[dict] = []

        for i, item in enumerate(videos):
            filename = item.get("filename", "video.mp4")
            prompt = item.get("prompt", "")
            has_ref = item.get("image") or item.get("first_frame") or item.get("input_reference")
            model = self._i2v_model if has_ref else self._t2v_model
            size = item.get("size", "1280x720")
            seconds = str(item.get("seconds", 4))

            create_params: dict[str, Any] = {
                "model": model,
                "prompt": prompt,
                "size": size,
                "seconds": seconds,
                "extra_body": {"filename": filename},
            }
            # Reference image
            ref = item.get("image") or item.get("input_reference")
            if ref:
                create_params["input_reference"] = ref
            if item.get("first_frame"):
                create_params["extra_body"]["first_frame"] = item["first_frame"]

            try:
                video = await client.videos.create(**create_params)
                video_id = getattr(video, "id", None) or (video.get("id") if isinstance(video, dict) else None)
                if not video_id:
                    raise RuntimeError("API returned empty video id")
                pre_urls = getattr(video, "pre_urls", None) or (video.get("pre_urls") if isinstance(video, dict) else []) or []
                submitted.append({
                    "task_id": video_id,
                    "filename": filename,
                    "create_params": create_params,
                    "pre_urls": pre_urls,
                    "status": getattr(video, "status", "queued") if not isinstance(video, dict) else video.get("status", "queued"),
                })
            except Exception as e:
                logger.error(f"Video submit failed for '{filename}': {e}")
                submitted.append({
                    "filename": filename,
                    "create_params": create_params,
                    "status": "failed",
                    "error": str(e),
                })
            if i < len(videos) - 1:
                await asyncio.sleep(0.5)

        immediate = {
            "submitted": [s for s in submitted if s.get("task_id")],
            "failed": [s for s in submitted if s.get("status") == "failed"],
            "message": f"{len([s for s in submitted if s.get('task_id')])} video tasks submitted.",
        }

        pending = [s for s in submitted if s.get("task_id") or s.get("create_params")]
        if not pending:
            return BgTaskResult.foreground(immediate)

        async def _poll() -> str:
            return await self._poll_all(pending)

        return BgTaskResult.hybrid(result=immediate, poll_factory=_poll, command_name="generate videos")

    async def _submit_one(self, client, create_params: dict) -> str:
        """Submit one video task, returning its id (raises on failure)."""
        video = await client.videos.create(**create_params)
        video_id = getattr(video, "id", None) or (video.get("id") if isinstance(video, dict) else None)
        if not video_id:
            raise RuntimeError("API returned empty video id")
        return video_id

    async def _resolve_one(self, client, item: dict) -> dict:
        """Resolve a single video asset: poll its task (re-submit if needed)."""
        filename = item["filename"]
        video_id = item.get("task_id")
        if not video_id:
            video_id = await self._submit_one(client, item["create_params"])
            item["task_id"] = video_id
        try:
            video = await self._poll_video(client, video_id)
        except Exception:
            item["task_id"] = None
            raise
        return {"status": "success", "filename": filename, "url": self._get_url(video)}

    async def _poll_all(self, pending: list[dict]) -> dict:
        client = self._client()
        results: list[dict] = []
        for item in pending:
            result = await _generate_one_with_retry(
                lambda it=item: self._resolve_one(client, it),
                filename=item["filename"],
                noun="Video",
            )
            results.append(result)
        if self._output_dir:
            async with aiohttp.ClientSession() as session:
                for item in results:
                    if item.get("status") != "success" or not item.get("url"):
                        continue
                    filename = item.get("filename") or "video.mp4"
                    dest = self._output_dir / filename
                    try:
                        await _download_to(session, item["url"], dest)
                        item["local_path"] = str(dest)
                    except Exception as e:
                        logger.warning(f"Video download failed for {filename}: {e}")
        return _summarize_poll_results(results, "videos")

    async def _poll_video(self, client, video_id: str, timeout: float = _POLL_TIMEOUT):
        elapsed = 0.0
        while True:
            video = await client.videos.retrieve(video_id)
            status = getattr(video, "status", None) or (video.get("status") if isinstance(video, dict) else "")
            if status == "completed":
                return video
            if status == "failed":
                err = getattr(video, "error", None) or (video.get("error") if isinstance(video, dict) else None)
                if isinstance(err, dict):
                    msg = err.get("message", "") or str(err)
                    code = str(err.get("code") or err.get("type") or "")
                else:
                    msg = getattr(err, "message", None) or str(err or "failed")
                    code = str(getattr(err, "code", "") or "")
                raise classify_media_failure(msg, task_id=video_id, code=code)
            if elapsed >= timeout:
                raise TimeoutError(f"Video {video_id} timed out after {timeout}s")
            await asyncio.sleep(_POLL_INTERVAL)
            elapsed += _POLL_INTERVAL

    @staticmethod
    def _get_url(video) -> str:
        """Extract URL from video response object."""
        if isinstance(video, dict):
            return (video.get("urls") or [video.get("url", "")])[0] or video.get("url", "")
        urls = getattr(video, "urls", None) or []
        if urls:
            return urls[0]
        return getattr(video, "url", "") or ""


# ---------------------------------------------------------------------------
# FfmpegComposer
# ---------------------------------------------------------------------------


class FfmpegComposer:
    """Compose the final video from generated clips + narration + music via FFmpeg.

    Pipeline (mirrors the proven manual flow):
      1. Concat the video clips in order into one continuous track.
      2. Lay each narration clip at the start offset of its matching video clip
         (offset = cumulative duration of preceding clips), then mix the
         narration segments into one track.
      3. Mix narration (full volume) with background music (ducked) and mux onto
         the concatenated video.

    Audio/video timing is unified to the video's total duration: the final
    output is trimmed to the concatenated video length so narration/music never
    run past the picture.
    """

    def __init__(self) -> None:
        self._ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        self._ffprobe = shutil.which("ffprobe") or "ffprobe"

    async def compose(
        self,
        *,
        videos: list[str],
        narration: Optional[list[str]] = None,
        music: Optional[list[str]] = None,
        output_path: str,
        bgm_volume: float = 0.25,
        narration_volume: float = 1.0,
        **_: Any,
    ) -> dict:
        """Stitch clips/narration/music into ``output_path``.

        Args:
            videos: Ordered list of local video clip paths.
            narration: Ordered narration clip paths (aligned to ``videos`` by
                index); each is placed at its clip's start offset.
            music: Background music path(s); first existing track is used.
            output_path: Destination mp4 path.
            bgm_volume: Background-music volume multiplier (ducked under narration).
            narration_volume: Narration volume multiplier.
        """
        if shutil.which(self._ffmpeg) is None and not os.path.isabs(self._ffmpeg):
            return {"status": "failed", "stage": "preflight",
                    "error": "ffmpeg not found on PATH."}

        clips = [v for v in (videos or []) if v and os.path.isfile(v)]
        if not clips:
            return {"status": "failed", "stage": "preflight",
                    "error": "No video clips available to compose."}

        narration = [n for n in (narration or []) if n and os.path.isfile(n)]
        music_track = next((m for m in (music or []) if m and os.path.isfile(m)), "")

        out_path = Path(output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        work_dir = out_path.parent
        log_path = work_dir / f"{out_path.stem}-ffmpeg.log"
        log_path.write_text("", encoding="utf-8")

        # 1. Concat video clips -> silent continuous video track.
        concat_path = work_dir / f"{out_path.stem}-video_concat.mp4"
        rc, durations = await self._concat_videos(clips, concat_path, log_path)
        if rc != 0:
            return {"status": "failed", "stage": "concat", "log_path": str(log_path),
                    "error": f"Video concat failed (exit {rc})."}
        total_video = sum(durations)

        # 2. Build the narration track placed at each clip's offset.
        narration_path = ""
        if narration:
            narration_path = str(work_dir / f"{out_path.stem}-narration.m4a")
            rc = await self._build_narration_track(
                narration, durations, narration_path, log_path
            )
            if rc != 0:
                logger.warning("Narration track build failed; composing without narration.")
                narration_path = ""

        # 3. Mux video + narration + music into the final output, trimmed to video length.
        rc = await self._mux_final(
            video=str(concat_path),
            narration=narration_path,
            music=music_track,
            total_video=total_video,
            output_path=str(out_path),
            bgm_volume=bgm_volume,
            narration_volume=narration_volume,
            log_path=log_path,
        )
        if rc != 0:
            return {"status": "failed", "stage": "mux", "log_path": str(log_path),
                    "output_path": str(out_path), "error": f"Final mux failed (exit {rc})."}

        # Best-effort cleanup of intermediates.
        for tmp in (concat_path, Path(narration_path) if narration_path else None):
            if tmp:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass

        if not out_path.exists() or out_path.stat().st_size == 0:
            return {"status": "failed", "stage": "verify", "output_path": str(out_path),
                    "log_path": str(log_path),
                    "error": "Compose completed but output is missing or empty."}

        return {
            "status": "success",
            "output_path": str(out_path),
            "log_path": str(log_path),
            "duration": round(total_video, 2),
            "clips": len(clips),
            "narration_clips": len(narration),
            "has_music": bool(music_track),
        }

    # -- internal steps ------------------------------------------------------

    async def _probe_duration(self, path: str) -> float:
        """Return media duration in seconds (0.0 on failure)."""
        cmd = [
            self._ffprobe, "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", path,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
            )
            out, _ = await proc.communicate()
            return float(out.decode().strip())
        except (ValueError, OSError):
            return 0.0

    async def _concat_videos(
        self, clips: list[str], concat_path: Path, log_path: Path
    ) -> tuple[int, list[float]]:
        """Concat clips via the concat demuxer; re-encode for safe joining.

        Returns (returncode, per-clip durations).
        """
        durations = [await self._probe_duration(c) for c in clips]
        list_file = concat_path.with_suffix(".txt")
        lines = "".join(f"file '{os.path.abspath(c)}'\n" for c in clips)
        list_file.write_text(lines, encoding="utf-8")

        # Re-encode (not -c copy): clips may differ in codec/timebase/fps, which
        # breaks stream-copy concat. Re-encoding guarantees a clean joined track.
        cmd = [
            self._ffmpeg, "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30",
            "-an", str(concat_path),
        ]
        rc = await self._run(cmd, log_path, stage="concat")
        try:
            list_file.unlink(missing_ok=True)
        except OSError:
            pass
        return rc, durations

    async def _build_narration_track(
        self, narration: list[str], durations: list[float],
        output_path: str, log_path: Path,
    ) -> int:
        """Place each narration clip at its matching clip's start offset, then mix.

        Narration i starts at sum(durations[:i]) — i.e. when clip i begins.
        Uses adelay (ms) per input and amix to combine.
        """
        offsets_ms: list[int] = []
        cumulative = 0.0
        for i in range(len(narration)):
            offsets_ms.append(int(cumulative * 1000))
            if i < len(durations):
                cumulative += durations[i]

        cmd: list[str] = [self._ffmpeg, "-y"]
        for n in narration:
            cmd += ["-i", n]

        parts = []
        labels = []
        for i, delay in enumerate(offsets_ms):
            parts.append(f"[{i}:a]adelay={delay}|{delay}[a{i}]")
            labels.append(f"[a{i}]")
        n = len(narration)
        if n == 1:
            filt = f"{parts[0]};[a0]anull[aout]"
        else:
            filt = ";".join(parts) + f";{''.join(labels)}amix=inputs={n}:normalize=0[aout]"
        cmd += ["-filter_complex", filt, "-map", "[aout]", "-c:a", "aac", output_path]
        return await self._run(cmd, log_path, stage="narration")

    async def _mux_final(
        self, *, video: str, narration: str, music: str,
        total_video: float, output_path: str,
        bgm_volume: float, narration_volume: float, log_path: Path,
    ) -> int:
        """Mux the concatenated video with narration + ducked music.

        Output audio is mixed and the whole output is trimmed to ``total_video``
        so audio and video share one unified duration.
        """
        cmd: list[str] = [self._ffmpeg, "-y", "-i", video]
        audio_inputs: list[tuple[str, float]] = []
        if narration:
            cmd += ["-i", narration]
            audio_inputs.append(("narration", narration_volume))
        if music:
            cmd += ["-i", music]
            audio_inputs.append(("music", bgm_volume))

        if not audio_inputs:
            # No audio at all — just trim the silent video.
            cmd += ["-c:v", "copy", "-t", f"{total_video:.3f}", output_path]
            return await self._run(cmd, log_path, stage="mux")

        # Audio input indices start at 1 (0 is the video).
        filt_parts = []
        labels = []
        for idx, (_kind, vol) in enumerate(audio_inputs, start=1):
            filt_parts.append(f"[{idx}:a]volume={vol}[v{idx}]")
            labels.append(f"[v{idx}]")
        if len(audio_inputs) == 1:
            filt = f"{filt_parts[0]};[v1]anull[aout]"
        else:
            filt = ";".join(filt_parts) + \
                f";{''.join(labels)}amix=inputs={len(audio_inputs)}:duration=longest:normalize=0[aout]"

        cmd += [
            "-filter_complex", filt,
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac",
            "-t", f"{total_video:.3f}",
            output_path,
        ]
        return await self._run(cmd, log_path, stage="mux")

    async def _run(self, cmd: list[str], log_path: Path, *, stage: str) -> int:
        """Run a subprocess, appending stdout/stderr to log_path."""
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n## {stage}\n$ {' '.join(cmd)}\n")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError as exc:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"Failed to start {stage} command: {exc}\n")
            return 127
        stdout, _ = await proc.communicate()
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(stdout.decode(errors="replace"))
        return proc.returncode or 0


