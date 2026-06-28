"""Multimodal service configurations (image/audio/music/video generation)."""
from __future__ import annotations

from typing import List

from pydantic import Field

from metagpt.common.utils.yaml_model import YamlModel


class ImageGenerationConfig(YamlModel):
    """Image generation service config."""

    api_key: str = ""
    base_url: str = ""
    model: str = "gpt-image-1.5"
    allowed_models: List[str] = Field(default_factory=list)
    max_concurrency: int = 4


class ImageUnderstandingConfig(YamlModel):
    """Image understanding / vision model config."""

    api_key: str = ""
    base_url: str = ""
    model: str = "claude-opus-4-6"
    max_concurrency: int = 4
    max_token: int = 8192
    temperature: float = 0.0
    timeout: int = 300


class AudioGenerationConfig(YamlModel):
    """TTS audio generation config."""

    api_key: str = ""
    base_url: str = ""
    model: str = "eleven_v3"
    max_concurrency: int = 4


class AudioTranscriptionConfig(YamlModel):
    """Audio transcription / STT config."""

    api_key: str = ""
    base_url: str = ""
    model: str = "scribe_v2"
    max_concurrency: int = 4


class MusicGenerationConfig(YamlModel):
    """Music generation config."""

    api_key: str = ""
    base_url: str = ""
    model: str = ""
    response_format: str = "url"
    max_concurrency: int = 2


class VideoGenerationConfig(YamlModel):
    """Video generation config."""

    api_key: str = ""
    base_url: str = ""
    text_to_video_model: str = "wan2.6-t2v"
    text_to_video_allowed_models: List[str] = Field(default_factory=list)
    reference_guided_video_model: str = "wan2.6-i2v"
    reference_guided_video_allowed_models: List[str] = Field(default_factory=list)
    video_edit_model: str = ""
    video_edit_allowed_models: List[str] = Field(default_factory=list)
    ad_film_allowed_models: List[str] = Field(default_factory=list)
    max_concurrency: int = 2


class PdfUnderstandingConfig(YamlModel):
    """PDF understanding / document analysis config."""

    api_key: str = ""
    base_url: str = ""
    model: str = "claude-sonnet-4-6"
    max_concurrency: int = 4
    max_token: int = 8192
    temperature: float = 0.0
    timeout: int = 600


class MultimodalConfig(YamlModel):
    """Top-level multimodal service configuration."""

    image_generation: ImageGenerationConfig = Field(default_factory=ImageGenerationConfig)
    image_understanding: ImageUnderstandingConfig = Field(default_factory=ImageUnderstandingConfig)
    audio_generation: AudioGenerationConfig = Field(default_factory=AudioGenerationConfig)
    audio_transcription: AudioTranscriptionConfig = Field(default_factory=AudioTranscriptionConfig)
    music_generation: MusicGenerationConfig = Field(default_factory=MusicGenerationConfig)
    video_generation: VideoGenerationConfig = Field(default_factory=VideoGenerationConfig)
    pdf_understanding: PdfUnderstandingConfig = Field(default_factory=PdfUnderstandingConfig)
