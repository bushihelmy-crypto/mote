"""Closed hosted-service operation payload and result contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mote.contracts.model.invocation import WebSearchHitOutput


class _FrozenOperation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class MediaKind(StrEnum):
    IMAGE = "image"
    AUDIO = "audio"
    MUSIC = "music"
    VIDEO = "video"


class MediaGenerationSpec(_FrozenOperation):
    """Validated superset of the four Product media request shapes."""

    description: str | None = None
    text: str | None = None
    prompt: str | None = None
    filename: str = ""
    size: str | None = None
    image: str | None = None
    input_reference: str | None = None
    first_frame: str | None = None
    gender: Literal["male", "female"] | None = None
    speed: float | None = Field(default=None, gt=0)
    seconds: int | None = Field(default=None, ge=1)
    negative_prompt: str | None = None
    seed: int | None = None
    lyrics: str | None = None
    audio_format: str | None = None
    sample_rate: int | None = Field(default=None, ge=1)
    bitrate: int | None = Field(default=None, ge=1)
    voice_id: str | None = None
    n: int = Field(default=1, ge=1)
    response_format: str | None = None


class MediaGenerationPayload(_FrozenOperation):
    kind: Literal["media_generation"] = "media_generation"
    media_kind: MediaKind
    item: MediaGenerationSpec

    @model_validator(mode="after")
    def _required_prompt(self) -> "MediaGenerationPayload":
        if self.media_kind is MediaKind.IMAGE and not (self.item.description or self.item.prompt):
            raise ValueError("image generation requires description or prompt")
        if self.media_kind is MediaKind.AUDIO and not self.item.text:
            raise ValueError("audio generation requires text")
        if self.media_kind in {MediaKind.MUSIC, MediaKind.VIDEO} and not self.item.prompt:
            raise ValueError(f"{self.media_kind.value} generation requires prompt")
        return self


class WebSearchPayload(_FrozenOperation):
    kind: Literal["web_search"] = "web_search"
    query: str = Field(min_length=1)
    allowed_domains: tuple[str, ...] = ()
    blocked_domains: tuple[str, ...] = ()
    max_uses: int = Field(default=8, ge=1)

    @model_validator(mode="after")
    def _exclusive_domains(self) -> "WebSearchPayload":
        if self.allowed_domains and self.blocked_domains:
            raise ValueError("allowed_domains and blocked_domains are mutually exclusive")
        return self


HostedServicePayload = Annotated[
    Union[MediaGenerationPayload, WebSearchPayload],
    Field(discriminator="kind"),
]


class MediaGenerationResult(_FrozenOperation):
    kind: Literal["media_generation"] = "media_generation"
    status: Literal["success"] = "success"
    filename: str
    url: str = ""
    urls: tuple[str, ...] = ()


class WebSearchResult(_FrozenOperation):
    kind: Literal["web_search"] = "web_search"
    hits: tuple[WebSearchHitOutput, ...] = ()


HostedServiceResult = Annotated[
    Union[MediaGenerationResult, WebSearchResult],
    Field(discriminator="kind"),
]


def capability_for_payload(payload: HostedServicePayload) -> str:
    if isinstance(payload, WebSearchPayload):
        return "web.search"
    return f"media.generate.{payload.media_kind.value}"


def route_for_payload(payload: HostedServicePayload) -> str:
    if isinstance(payload, WebSearchPayload):
        return "web.search"
    return f"media.{payload.media_kind.value}"


__all__ = [
    "HostedServicePayload",
    "HostedServiceResult",
    "MediaGenerationPayload",
    "MediaGenerationResult",
    "MediaGenerationSpec",
    "MediaKind",
    "WebSearchPayload",
    "WebSearchResult",
    "capability_for_payload",
    "route_for_payload",
]
