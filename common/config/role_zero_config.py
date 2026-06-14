from typing import Any, Literal

from pydantic import Field, model_validator

from metagpt.common.utils.yaml_model import YamlModel

# Mapping from legacy flat config keys to (section, nested_key). Defined at
# module level so Pydantic's private-attr machinery doesn't swallow it if
# declared inside the class (underscore-prefixed class attributes on a
# BaseModel become ModelPrivateAttr descriptors).
_FLAT_TO_NESTED_ALIASES: dict[str, tuple[str, str]] = {
    "enable_skills": ("skills", "enabled"),
    "max_skill_tokens": ("skills", "max_tokens"),
}


class SkillsConfig(YamlModel):
    """P0 Skills subsystem configuration."""

    enabled: bool = Field(default=False, description="P0 Skills master switch.")
    max_tokens: int = Field(default=2000, description="Token limit for alwaysApply + index injection.")


class RoleZeroConfig(YamlModel):
    ai_capability_models: list[str] = Field(
        default_factory=lambda: [
            "gpt-5.4 [gentxt] (Versatile / Multimodal / Structured Writing)",
            "claude-opus-4.6 [gentxt] (Code Expert / Multimodal / High Quality)",
            "deepseek-v4-pro [gentxt] (Cost Effective / Text Only / Bulk Processing)",
            "gemini-2.5-pro [gentxt] (Multimodal / Production Grade / General Purpose)",
            "gemini-3.1-pro-preview [gentxt] (Best Multimodal / Long Context)",
            "gpt-image-2 [genimg] (Image Generation / Visual Intelligence / Sharp Detail) [recommended image generation model]",
            "gemini-3-pro-image-preview [genimg] (Best Quality / Precise Text Rendering)",
            "gemini-3.1-flash-image-preview [genimg] (High Quality / Cost Effective)",
            "wan2.6-t2v [genvideo] (Text to Video / Cost Effective / Up to 15 Seconds)",
            "wan2.6-i2v [genvideo] (Image to Video / Cost Effective / Up to 15 Seconds)",
            "wan2.7-r2v [genvideo] (Reference Images to Video)",
            "wan2.7-videoedit [genvideo] (Video Editing / Strict Source Handling)",
            "veo-3.1-generate-001 [genvideo] (Text/Image to Video / High Quality / Cinematic Realism)",
            "seedance-1-5-pro [genvideo] (Text/Image to Video / Lightning Fast / Multilingual)",
            "seedance-2.0 [genvideo] (Reference to Video / Video Generation)",
            "seedance-2.0-fast [genvideo] (Fast Reference to Video)",
            "happyhorse-1.0-r2v [genvideo] (Reference Image to Video)",
            "happyhorse-1.0-video-edit [genvideo] (Video Editing / Recommended Default)",
            "eleven_v3 [genaudio] (Text to Speech / High Quality / 70+ Languages)",
            "qwen3-tts-flash [genaudio] (Text to Speech / Lightning Fast / Cost Effective)",
            "gemini-2.5-pro-preview-tts [genaudio] (Text to Speech / Natural Voice / 30+ Languages)",
            "lyria-3-pro-preview [genmusic] (Text to Music / High Quality Instrumental BGM)",
            "lyria-3-clip-preview [genmusic] (Text to Music / Fast Preview)",
            "scribe_v2 [transcribe] (Speech Recognition / High Accuracy / 90+ Languages)",
        ],
        description="Supported AI models exposed in the draft-plan prompt for AI-capability tasks.",
    )
    
    enable_longterm_memory: bool = Field(default=False, description="Whether to use long-term memory.")
    longterm_memory_persist_path: str = Field(default=".role_memory_data", description="The directory to save data.")
    memory_k: int = Field(default=30, description="The capacity of short-term memory.")
    similarity_top_k: int = Field(default=5, description="The number of long-term memories to retrieve.")
    use_llm_ranker: bool = Field(default=False, description="Whether to use LLM Reranker to get better result.")

    enable_compressable_memory: bool = Field(default=False, description="Whether to use compressable memory.")
    compress_type: Literal["compaction", "compressable"] = Field(
        default="compaction",
        description="Compression type when enable_compressable_memory is True. "
        "'compaction': AdaptiveCompactionMemory (token-based compression), "
        "'compressable': CompressableMemory (message-count based compression).",
    )
    compress_k: int = Field(default=15, description="Number of latest messages to keep in the compressed memory.")
    compress_model_name: str = Field(default="gpt-4o-mini", description="Model to use for compression.")
    compress_trigger_token_threshold: int = Field(
        default=0,
        description="Compression will be automatically triggered if the value is exceeded. A value of 0 will be ineffective",
    )

    # AdaptiveCompactionMemory configuration
    max_context_tokens: int = Field(default=128000, description="Max context window size in tokens (e.g., 128K).")
    compression_threshold: float = Field(default=0.8, description="Token usage ratio to trigger compression (0.0-1.0).")
    target_after_compression: float = Field(
        default=0.50, description="Target token usage ratio after compression (0.0-1.0)."
    )
    protected_recent_messages: int = Field(
        default=8, description="Number of recent messages to protect from compression."
    )
    min_compression_ratio: float = Field(
        default=0.08, description="Min compression ratio (0.0-1.0), switch to Summary if last compression < this."
    )

    # P0 Skills
    skills: SkillsConfig = Field(default_factory=SkillsConfig)

    # --- Backward-compat migration for pre-nested flat config ---
    # Older configs used flat fields (enable_skills / max_skill_tokens).
    # This validator folds them into the new nested subsections so upgrades
    # don't silently lose settings. Nested values take precedence if both are
    # supplied in the same config.
    @model_validator(mode="before")
    @classmethod
    def _migrate_flat_aliases(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        for flat_key, (section, nested_key) in _FLAT_TO_NESTED_ALIASES.items():
            if flat_key not in values:
                continue
            flat_value = values.pop(flat_key)
            sub = values.setdefault(section, {})
            # Only honor the flat value when the nested key isn't explicitly
            # set — nested form is the source of truth going forward.
            if isinstance(sub, dict) and nested_key not in sub:
                sub[nested_key] = flat_value
        return values

    # --- Read-only accessors for callers that still use the flat names ---

    @property
    def enable_skills(self) -> bool:
        return self.skills.enabled

    @property
    def max_skill_tokens(self) -> int:
        return self.skills.max_tokens
