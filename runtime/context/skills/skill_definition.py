"""Skill definition data model."""

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from mote.contracts.models.tokenization import count_string_tokens

NAME_PATTERN = re.compile(r"^[a-z0-9-]{1,64}$")

# Valid execution modes for a skill. ``inline`` returns the rendered body as a
# tool result (the model reads it in the main conversation); ``fork`` runs the
# body inside a fresh isolated child agent and returns only its final summary.
ContextMode = Literal["inline", "fork"]


class SkillDefinition(BaseModel):
    """Represents a single Skill parsed from a SKILL.md file.

    The first four fields (name/description/globs/instructions) are the
    original schema; the rest mirror the SKILL.md frontmatter
    (when_to_use / context / allowed-tools / model / effort / argument-hint /
    disable_model_invocation / paths) and all carry defaults so older skills
    keep parsing unchanged.
    """

    name: str = ""
    description: str = Field(default="", max_length=1024)
    globs: list[str] = Field(default_factory=list)
    instructions: str = ""
    source_path: Path = Field(default_factory=lambda: Path())
    token_count: int = 0
    metadata: dict = Field(default_factory=dict)

    # --- supported frontmatter (all optional) ---
    # Trigger description shown in the index (when the model should reach for
    # this skill). Complements ``description``.
    when_to_use: str = ""
    # Execution mode: "inline" (body returned as tool result) or "fork" (body
    # runs inside an isolated child agent, only the summary returns).
    context: ContextMode = "inline"
    # Tool whitelist for the fork child agent (caps its capabilities).
    allowed_tools: list[str] = Field(default_factory=list)
    # Model / effort overrides for the fork child agent.
    model: str = ""
    effort: str = ""
    # Argument hint shown in the index (how to fill ``arguments``).
    argument_hint: str = ""
    # When True the skill is human-invocable only (hidden from model autoload).
    disable_model_invocation: bool = False
    # Path patterns for conditional activation (merged with ``globs``).
    paths: list[str] = Field(default_factory=list)

    @field_validator("context", mode="before")
    @classmethod
    def _coerce_context(cls, value):
        """Fall back to ``inline`` for missing/invalid context values."""
        if value not in ("inline", "fork"):
            return "inline"
        return value

    def model_post_init(self, __context):
        if self.instructions and self.token_count == 0:
            self.token_count = count_string_tokens(self.instructions, "gpt-4o")

    def is_valid(self) -> bool:
        """Check if this SkillDefinition has a valid name and description."""
        return bool(NAME_PATTERN.match(self.name) and self.description)

    @property
    def activation_patterns(self) -> list[str]:
        """All path patterns that conditionally activate this skill.

        Merges ``paths`` and the ``globs`` alias (deduped, order-preserving).
        """
        seen: set[str] = set()
        merged: list[str] = []
        for pat in [*self.paths, *self.globs]:
            if pat and pat not in seen:
                seen.add(pat)
                merged.append(pat)
        return merged

    @property
    def is_conditional(self) -> bool:
        """True when this skill is gated behind path/glob activation patterns."""
        return bool(self.paths or self.globs)
