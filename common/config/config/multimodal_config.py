"""Multimodal service configurations (image/audio/music/video generation).

Only the four generation services the GenerateMedia tool actually consumes are
modelled (see ``executor/tools/generate_media/creators.py``). Each carries just
the endpoint + model fields the creators read.
"""
from __future__ import annotations

from pydantic import Field

from mote.common.utils.yaml_model import YamlModel


class ImageGenerationConfig(YamlModel):
    """Image generation service config."""

    api_key: str = ""
    base_url: str = ""
    model: str = "gpt-image-1.5"


class AudioGenerationConfig(YamlModel):
    """TTS audio generation config."""

    api_key: str = ""
    base_url: str = ""
    model: str = "eleven_v3"


class MusicGenerationConfig(YamlModel):
    """Music generation config."""

    api_key: str = ""
    base_url: str = ""
    model: str = ""
    response_format: str = "url"


class VideoGenerationConfig(YamlModel):
    """Video generation config."""

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
