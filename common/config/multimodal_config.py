from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

AD_FILM_MODEL = "vidu-ad-one-click"
IMAGE_GENERATION_ALLOWED_MODELS = [
    "gemini-2.5-flash-image",
    "gemini-2.5-flash-image-preview",
    "gemini-3-pro-image-preview",
    "gemini-3.1-flash-image-preview",
    "gpt-image-1.5",
    "gpt-image-2",
]
TEXT_TO_VIDEO_ALLOWED_MODELS = [
    "wan2.6-t2v",
    "veo-3.1-generate-001",
    "seedance-1-5-pro",
    "seedance-2.0",
    "seedance-2.0-fast",
]
REFERENCE_GUIDED_VIDEO_ALLOWED_MODELS = [
    "wan2.6-i2v",
    "wan2.7-r2v",
    "veo-3.1-generate-001",
    "seedance-1-5-pro",
    "seedance-2.0",
    "seedance-2.0-fast",
    "happyhorse-1.0-r2v",
]
VIDEO_EDIT_ALLOWED_MODELS = [
    "happyhorse-1.0-video-edit",
    "wan2.7-videoedit",
]
AUDIO_GENERATION_ALLOWED_MODELS = [
    "eleven_v3",
    "eleven_turbo_v2",
    "qwen3-tts-flash",
    "gemini-2.5-pro-preview-tts",
    "gpt-4o-mini-tts",
]
MUSIC_GENERATION_ALLOWED_MODELS = [
    "lyria-3-pro-preview",
    "lyria-3-clip-preview",
    "minimax-music-1.5",
]


class MultimodalAIServiceConfig(BaseModel):
    """Shared provider config for AI-powered multimodal capabilities."""

    base_url: str = ""
    api_key: str = ""
    max_concurrency: int = 4


class MultimodalAICapabilityConfig(MultimodalAIServiceConfig):
    """Capability-level AI config that can override shared multimodal AI settings."""


class ImageGenerationConfig(MultimodalAICapabilityConfig):
    """Config for Image Generation"""

    model: str = "gemini-2.5-flash-image"
    allowed_models: list[str] = Field(default_factory=lambda: IMAGE_GENERATION_ALLOWED_MODELS.copy())
    chart_size: str = "1024x1024"
    image_size: str = "1024x1024"


class ImageUnderstandingConfig(MultimodalAICapabilityConfig):
    """Config for image understanding and OCR."""

    model: str = "gemini-3-flash-preview"
    temperature: float = 0.0
    max_token: int = 8192
    timeout: int = 300


class VideoGenerationConfig(MultimodalAICapabilityConfig):
    """Config for Video Generation"""

    model_config = ConfigDict(extra="forbid")

    text_to_video_model: str = "seedance-2.0"
    text_to_video_allowed_models: list[str] = Field(default_factory=lambda: TEXT_TO_VIDEO_ALLOWED_MODELS.copy())
    reference_guided_video_model: str = "seedance-2.0"
    reference_guided_video_allowed_models: list[str] = Field(
        default_factory=lambda: REFERENCE_GUIDED_VIDEO_ALLOWED_MODELS.copy()
    )
    video_edit_model: str = "happyhorse-1.0-video-edit"
    video_edit_allowed_models: list[str] = Field(default_factory=lambda: VIDEO_EDIT_ALLOWED_MODELS.copy())
    ad_film_allowed_models: list[str] = Field(default_factory=lambda: [AD_FILM_MODEL])
    max_concurrency: int = 2

    @model_validator(mode="before")
    @classmethod
    def reject_legacy_video_fields(cls, values):
        if not isinstance(values, dict):
            return values
        legacy_fields = [field for field in ("image_to_video_model", "image_to_video_allowed_models") if field in values]
        if legacy_fields:
            joined = ", ".join(legacy_fields)
            raise ValueError(
                f"Legacy video_generation field(s) not supported anymore: {joined}. "
                "Use reference_guided_video_model and reference_guided_video_allowed_models instead."
            )
        return values


class ChartGenerationConfig(BaseModel):
    """Config for Kroki-based chart and diagram generation."""

    base_url: str = "https://kroki.io"
    timeout_seconds: int = 30
    max_concurrency: int = 8


class AudioGenerationConfig(MultimodalAICapabilityConfig):
    """Config for Audio Generation"""

    model: str = "eleven_v3"
    allowed_models: list[str] = Field(default_factory=lambda: AUDIO_GENERATION_ALLOWED_MODELS.copy())


class MusicGenerationConfig(MultimodalAICapabilityConfig):
    """Config for Music Generation"""

    model: str = "lyria-3-pro-preview"
    allowed_models: list[str] = Field(default_factory=lambda: MUSIC_GENERATION_ALLOWED_MODELS.copy())
    response_format: str = "url"


class AudioTranscriptionConfig(MultimodalAICapabilityConfig):
    """Config for Audio Transcription"""

    model: str = "scribe_v2"


class PdfUnderstandingConfig(MultimodalAICapabilityConfig):
    """Config for PDF understanding."""

    model: str = "claude-sonnet-4.6"
    temperature: float = 0.0
    max_token: int = 8192
    timeout: int = 600


class MultimodalConfig(BaseModel):
    """Unified config for multimodal capabilities."""

    ai_service: MultimodalAIServiceConfig = Field(
        default_factory=MultimodalAIServiceConfig,
        validation_alias=AliasChoices("ai_service", "ai"),
    )
    image_generation: ImageGenerationConfig = Field(default_factory=ImageGenerationConfig)
    image_understanding: ImageUnderstandingConfig = Field(default_factory=ImageUnderstandingConfig)
    video_generation: VideoGenerationConfig = Field(default_factory=VideoGenerationConfig)
    chart_generation: ChartGenerationConfig = Field(default_factory=ChartGenerationConfig)
    audio_generation: AudioGenerationConfig = Field(default_factory=AudioGenerationConfig)
    music_generation: MusicGenerationConfig = Field(default_factory=MusicGenerationConfig)
    audio_transcription: AudioTranscriptionConfig = Field(default_factory=AudioTranscriptionConfig)
    pdf_understanding: PdfUnderstandingConfig = Field(default_factory=PdfUnderstandingConfig)

    @staticmethod
    def _apply_shared_ai_service_defaults(
        capability_config: MultimodalAICapabilityConfig,
        shared_ai_config: MultimodalAIServiceConfig,
        *,
        inherit_max_concurrency: bool = True,
    ) -> None:
        if not capability_config.base_url:
            capability_config.base_url = shared_ai_config.base_url
        if not capability_config.api_key:
            capability_config.api_key = shared_ai_config.api_key
        if inherit_max_concurrency and "max_concurrency" not in capability_config.model_fields_set:
            capability_config.max_concurrency = shared_ai_config.max_concurrency

    def inherit_ai_service_defaults(self):
        for capability_config, inherit_max_concurrency in (
            (self.image_generation, True),
            (self.image_understanding, True),
            (self.video_generation, False),
            (self.audio_generation, True),
            (self.music_generation, True),
            (self.audio_transcription, True),
            (self.pdf_understanding, True),
        ):
            self._apply_shared_ai_service_defaults(
                capability_config,
                self.ai_service,
                inherit_max_concurrency=inherit_max_concurrency,
            )
        return self

    @staticmethod
    def _ensure_default_in_allowed(model: str, allowed_models: list[str], capability_name: str) -> None:
        if model not in allowed_models:
            allowed = ", ".join(allowed_models)
            raise ValueError(
                f"{capability_name} default model '{model}' must be included in allowed_models: {allowed}"
            )

    @model_validator(mode="after")
    def apply_shared_ai_config(self):
        self.inherit_ai_service_defaults()
        self._ensure_default_in_allowed(
            self.image_generation.model,
            self.image_generation.allowed_models,
            "multimodal.image_generation",
        )
        self._ensure_default_in_allowed(
            self.audio_generation.model,
            self.audio_generation.allowed_models,
            "multimodal.audio_generation",
        )
        self._ensure_default_in_allowed(
            self.music_generation.model,
            self.music_generation.allowed_models,
            "multimodal.music_generation",
        )
        self._ensure_default_in_allowed(
            self.video_generation.text_to_video_model,
            self.video_generation.text_to_video_allowed_models,
            "multimodal.video_generation.text_to_video",
        )
        self._ensure_default_in_allowed(
            self.video_generation.reference_guided_video_model,
            self.video_generation.reference_guided_video_allowed_models,
            "multimodal.video_generation.reference_guided_video",
        )
        self._ensure_default_in_allowed(
            self.video_generation.video_edit_model,
            self.video_generation.video_edit_allowed_models,
            "multimodal.video_generation.video_edit",
        )
        if AD_FILM_MODEL not in self.video_generation.ad_film_allowed_models:
            allowed = ", ".join(self.video_generation.ad_film_allowed_models)
            raise ValueError(
                f"multimodal.video_generation.ad_film_allowed_models must include '{AD_FILM_MODEL}': {allowed}"
            )
        return self
