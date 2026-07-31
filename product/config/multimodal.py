"""Multimodal service configurations (image/audio/music/video generation).

Only the four generation services the GenerateMedia tool actually consumes are
modelled (see ``executor/tools/generate_media/creators.py``). Each carries just
the endpoint + model fields the creators read.
"""
from __future__ import annotations

from pydantic import Field

from mote.product.config.base import ConfigModel as YamlModel


class ImageGenerationConfig(YamlModel):
    """Image generation service config."""

    # Which registered MediaProvider drives this kind (see
    # ``executor/tools/generate_media/registry.py``). The built-in
    # OpenAI-compatible async-task backend is ``"openai"``; point this at another
    # registered provider name to swap the vendor.
    provider: str = "openai"
    api_key: str = ""
    base_url: str = ""
    model: str = "gpt-image-1.5"


class AudioGenerationConfig(YamlModel):
    """TTS audio generation config."""

    provider: str = "openai"
    api_key: str = ""
    base_url: str = ""
    model: str = "eleven_v3"


class MusicGenerationConfig(YamlModel):
    """Music generation config."""

    provider: str = "openai"
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    response_format: str = "url"


class VideoGenerationConfig(YamlModel):
    """Video generation config."""

    provider: str = "openai"
    api_key: str = ""
    base_url: str = ""
    text_to_video_model: str = "wan2.6-t2v"
    reference_guided_video_model: str = "wan2.6-i2v"


class MultimodalConfig(YamlModel):
    """Top-level multimodal service configuration."""

    image_generation: ImageGenerationConfig = Field(default_factory=ImageGenerationConfig)
    audio_generation: AudioGenerationConfig = Field(default_factory=AudioGenerationConfig)
    music_generation: MusicGenerationConfig = Field(default_factory=MusicGenerationConfig)
    video_generation: VideoGenerationConfig = Field(default_factory=VideoGenerationConfig)
