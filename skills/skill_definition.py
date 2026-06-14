"""Skill definition data model."""

import re
from pathlib import Path

from pydantic import BaseModel, Field

from metagpt.common.utils import count_string_tokens

NAME_PATTERN = re.compile(r"^[a-z0-9-]{1,64}$")


class SkillDefinition(BaseModel):
    """Represents a single Skill parsed from a SKILL.md file."""

    name: str = ""
    description: str = Field(default="", max_length=1024)
    always_apply: bool = False
    globs: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    instructions: str = ""
    source_path: Path = Field(default_factory=lambda: Path())
    token_count: int = 0
    metadata: dict = Field(default_factory=dict)

    def model_post_init(self, __context):
        if self.instructions and self.token_count == 0:
            self.token_count = count_string_tokens(self.instructions, "gpt-4o")

    def is_valid(self) -> bool:
        """Check if this SkillDefinition has a valid name and description."""
        return bool(NAME_PATTERN.match(self.name) and self.description)
