"""Built-in single-wire media endpoint implementations."""

from __future__ import annotations

import os
from typing import Any

import aiohttp

try:
    from openai import AsyncOpenAI
except ImportError:  # pragma: no cover - optional provider extra
    AsyncOpenAI = None

from mote.product.media_generation.errors import classify_media_failure
from mote.product.media_generation.registry import MediaProvider, media_provider


def _headers(api_key: str, idempotency_key: str | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


async def _post_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload, timeout=timeout) as response:
            response.raise_for_status()
            return await response.json()


async def _poll_task_once(
    base_url: str,
    api_key: str,
    operation_id: str,
    timeout_seconds: float,
) -> dict[str, Any] | None:
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{base_url}/async-tasks/{operation_id}",
            headers=_headers(api_key),
            timeout=timeout,
        ) as response:
            response.raise_for_status()
            data = await response.json()
    status = str(data.get("status") or "").lower()
    if status == "completed":
        return data
    if status == "failed":
        _raise_task_failure(data, operation_id)
    return None


def _raise_task_failure(data: dict[str, Any], operation_id: str) -> None:
    error = data.get("error")
    if isinstance(error, dict):
        message = str(error.get("message") or error)
        code = str(error.get("code") or error.get("type") or data.get("code") or "")
    else:
        message = str(error or "media generation failed")
        code = str(data.get("code") or "")
    raise classify_media_failure(message, task_id=operation_id, code=code)


def _operation_id(data: dict[str, Any]) -> str:
    operation_id = str(data.get("id") or data.get("task_id") or "")
    if not operation_id:
        raise RuntimeError("media service returned an empty operation id")
    return operation_id


def _completed_asset(data: dict[str, Any], filename: str) -> dict[str, Any]:
    urls = data.get("urls") or []
    if not urls and data.get("url"):
        urls = [data["url"]]
    if not urls:
        urls = data.get("pre_urls") or []
    normalized = [str(url) for url in urls if url]
    return {
        "status": "success",
        "filename": filename,
        "urls": normalized,
        "url": normalized[0] if normalized else "",
    }


@media_provider("audio", "openai")
class AudioCreator(MediaProvider):
    """OpenAI-compatible asynchronous TTS endpoint."""

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

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        self._api_key = str(config.api_key)
        self._base_url = str(config.base_url).rstrip("/")
        self._model = str(config.model)

    async def start_once(
        self,
        item: dict[str, Any],
        *,
        idempotency_key: str,
        timeout_seconds: float,
    ) -> str:
        gender = str(item.get("gender") or "female")
        payload = {
            "model": self._model,
            "input": item.get("text") or item.get("input") or "",
            "voice": self.VOICE_MAP.get((self._model, gender)) or ("echo" if gender == "male" else "alloy"),
            "speed": item.get("speed", 1.0),
            "response_format": "mp3",
            "filename": item.get("filename", "audio.mp3"),
        }
        data = await _post_json(
            f"{self._base_url}/audio/speech/async",
            _headers(self._api_key, idempotency_key),
            payload,
            timeout_seconds,
        )
        return _operation_id(data)

    async def poll_once(
        self,
        operation_id: str,
        state: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> dict[str, Any] | None:
        data = await _poll_task_once(self._base_url, self._api_key, operation_id, timeout_seconds)
        return None if data is None else _completed_asset(data, str(state["filename"]))


@media_provider("music", "openai")
class MusicCreator(MediaProvider):
    """OpenAI-compatible asynchronous music endpoint."""

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        self._api_key = str(config.api_key)
        self._base_url = str(config.base_url).rstrip("/")
        self._model = str(config.model)
        self._response_format = str(config.response_format or "url")

    async def start_once(
        self,
        item: dict[str, Any],
        *,
        idempotency_key: str,
        timeout_seconds: float,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self._model,
            "prompt": item.get("prompt", ""),
            "n": item.get("n", 1),
            "response_format": item.get("response_format") or self._response_format,
            "filename": item.get("filename", "music.wav"),
        }
        for key in (
            "negative_prompt",
            "seed",
            "lyrics",
            "audio_format",
            "sample_rate",
            "bitrate",
            "voice_id",
        ):
            if item.get(key) is not None:
                payload[key] = item[key]
        data = await _post_json(
            f"{self._base_url}/audio/music/async",
            _headers(self._api_key, idempotency_key),
            payload,
            timeout_seconds,
        )
        return _operation_id(data)

    async def poll_once(
        self,
        operation_id: str,
        state: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> dict[str, Any] | None:
        data = await _poll_task_once(self._base_url, self._api_key, operation_id, timeout_seconds)
        return None if data is None else _completed_asset(data, str(state["filename"]))


@media_provider("image", "openai")
class ImageCreator(MediaProvider):
    """OpenAI-compatible image generation and editing endpoint."""

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        self._api_key = str(config.api_key)
        self._base_url = str(config.base_url).rstrip("/")
        self._model = str(config.model)

    async def start_once(
        self,
        item: dict[str, Any],
        *,
        idempotency_key: str,
        timeout_seconds: float,
    ) -> str:
        filename = str(item.get("filename") or "image.png")
        prompt = item.get("description") or item.get("prompt") or ""
        size = str(item.get("size") or "1024x1024")
        reference = item.get("image")
        if reference:
            data = await self._submit_edit_once(
                prompt=str(prompt),
                filename=filename,
                size=size,
                reference=str(reference),
                idempotency_key=idempotency_key,
                timeout_seconds=timeout_seconds,
            )
        else:
            data = await _post_json(
                f"{self._base_url}/images/generations/async",
                _headers(self._api_key, idempotency_key),
                {
                    "model": self._model,
                    "prompt": prompt,
                    "n": 1,
                    "size": size,
                    "filename": filename,
                },
                timeout_seconds,
            )
        return _operation_id(data)

    async def _submit_edit_once(
        self,
        *,
        prompt: str,
        filename: str,
        size: str,
        reference: str,
        idempotency_key: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        async with aiohttp.ClientSession() as session:
            if reference.startswith(("http://", "https://")):
                async with session.get(reference, timeout=timeout) as response:
                    response.raise_for_status()
                    image = await response.read()
                reference_name = "reference.png"
            else:
                with open(reference, "rb") as stream:
                    image = stream.read()
                reference_name = os.path.basename(reference)
            form = aiohttp.FormData()
            form.add_field("model", self._model)
            form.add_field("prompt", prompt)
            form.add_field("n", "1")
            form.add_field("size", size)
            form.add_field("filename", filename)
            form.add_field(
                "image",
                image,
                filename=reference_name,
                content_type="image/png",
            )
            headers = _headers(self._api_key, idempotency_key)
            headers.pop("Content-Type", None)
            async with session.post(
                f"{self._base_url}/images/edits/async",
                headers=headers,
                data=form,
                timeout=timeout,
            ) as response:
                response.raise_for_status()
                return await response.json()

    async def poll_once(
        self,
        operation_id: str,
        state: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> dict[str, Any] | None:
        data = await _poll_task_once(self._base_url, self._api_key, operation_id, timeout_seconds)
        return None if data is None else _completed_asset(data, str(state["filename"]))


@media_provider("video", "openai")
class VideoCreator(MediaProvider):
    """OpenAI SDK video generation endpoint."""

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        self._api_key = str(config.api_key)
        self._base_url = str(config.base_url).rstrip("/")
        self._text_model = str(config.text_to_video_model)
        self._reference_model = str(config.reference_guided_video_model)

    def _client(self):
        if AsyncOpenAI is None:
            raise RuntimeError("OpenAI video generation requires the 'openai' optional dependency")
        return AsyncOpenAI(
            api_key=self._api_key,
            base_url=self._base_url,
            max_retries=0,
        )

    async def start_once(
        self,
        item: dict[str, Any],
        *,
        idempotency_key: str,
        timeout_seconds: float,
    ) -> str:
        reference = item.get("image") or item.get("input_reference")
        has_reference = reference or item.get("first_frame")
        params: dict[str, Any] = {
            "model": self._reference_model if has_reference else self._text_model,
            "prompt": item.get("prompt", ""),
            "size": item.get("size", "1280x720"),
            "seconds": str(item.get("seconds", 4)),
            "extra_body": {"filename": item.get("filename", "video.mp4")},
            "extra_headers": {"Idempotency-Key": idempotency_key},
            "timeout": timeout_seconds,
        }
        if reference:
            params["input_reference"] = reference
        if item.get("first_frame"):
            params["extra_body"]["first_frame"] = item["first_frame"]
        client = self._client()
        try:
            video = await client.videos.create(**params)
            return _video_id(video)
        finally:
            await client.close()

    async def poll_once(
        self,
        operation_id: str,
        state: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> dict[str, Any] | None:
        client = self._client()
        try:
            video = await client.videos.retrieve(
                operation_id,
                timeout=timeout_seconds,
            )
        finally:
            await client.close()
        status = str(_field(video, "status") or "").lower()
        if status == "failed":
            error = _field(video, "error")
            if isinstance(error, dict):
                message = str(error.get("message") or error)
                code = str(error.get("code") or error.get("type") or "")
            else:
                message = str(getattr(error, "message", None) or error or "failed")
                code = str(getattr(error, "code", "") or "")
            raise classify_media_failure(
                message,
                task_id=operation_id,
                code=code,
            )
        if status != "completed":
            return None
        urls = _field(video, "urls") or []
        url = str((urls[0] if urls else _field(video, "url")) or "")
        return {
            "status": "success",
            "filename": str(state["filename"]),
            "url": url,
            "urls": [url] if url else [],
        }


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _video_id(video: Any) -> str:
    operation_id = str(_field(video, "id") or "")
    if not operation_id:
        raise RuntimeError("media service returned an empty video operation id")
    return operation_id


__all__ = ["AudioCreator", "ImageCreator", "MusicCreator", "VideoCreator"]
