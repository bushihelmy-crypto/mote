from typing import Any

from mote.common.utils.yaml_model import YamlModel
from pydantic import Field, model_validator

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
    max_tokens: int = Field(default=2000, description="Token limit for the Skills index injection.")
    # Layered source directories (precedence-as-data: bundled < user < project
    # < extra). The user toggle adds ``~/.mote/skills``; the project toggle adds
    # every ``<dir>/.mote/skills`` found walking from cwd up to the git root
    # (Claude-Code-aligned). ``extra_dirs`` appends arbitrary highest-priority
    # directories. Same-name skills in a higher layer override lower ones.
    include_user_dir: bool = Field(default=True, description="Scan ~/.mote/skills for user-level skills.")
    include_project_dir: bool = Field(
        default=True, description="Scan <dir>/.mote/skills (cwd→git-root walk) for project-level skills."
    )
    extra_dirs: list[str] = Field(
        default_factory=list, description="Additional (highest-priority) skill source directories."
    )


class RoleConfig(YamlModel):
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

    # Adaptive (token-based) compaction. When enabled, the think-engine emits the
    # compaction-aware prompt sections (Function Result Clearing / summarize /
    # task-final-output) that tell the model old tool results get cleared. The
    # actual clearing is run by ``context.compaction`` (ContextManager); these two
    # knobs only shape the prompt. ``protected_recent_messages`` = how many recent
    # messages the FRC section says are kept intact.
    enable_compressable_memory: bool = Field(default=False, description="Whether to use adaptive compaction memory.")
    protected_recent_messages: int = Field(
        default=8, description="Number of recent messages to protect from compression."
    )

    # P0 Skills
    skills: SkillsConfig = Field(default_factory=SkillsConfig)

    # Code map (P3): opportunistically surface real per-symbol callers of calm
    # public symbols (``foo called by: a.py``) via the LSP references facade, not
    # only when an interface breaks. Default off — it adds LSP ``references``
    # volume, so it is opt-in; no effect unless an LSP layer is configured.
    code_map_surface_callers: bool = Field(
        default=False, description="Surface per-symbol callers of calm public symbols in the code map (needs LSP)."
    )

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
