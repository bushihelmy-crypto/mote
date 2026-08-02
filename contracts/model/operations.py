"""Finite provider-neutral operations admitted by ModelGateway endpoints."""

from enum import StrEnum


class ModelOperation(StrEnum):
    GENERATE = "generate"
    EMBEDDING = "embedding"
    IMAGE_GENERATION = "image_generation"
    SPEECH = "speech"
    TRANSCRIPTION = "transcription"
    WEB_SEARCH = "web_search"
    IMAGE_DESCRIPTION = "image_description"


__all__ = ["ModelOperation"]
