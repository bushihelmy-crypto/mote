"""Frozen Product-owned Skill manifest, source evidence, and activation snapshot."""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

NAME_PATTERN = re.compile(r"^[a-z0-9-]{1,64}$")


class SkillContext(StrEnum):
    INLINE = "inline"
    FORK = "fork"


class SkillManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    name: str = Field(pattern=r"^[a-z0-9-]{1,64}$")
    description: str = Field(min_length=1, max_length=1024)
    when_to_use: str = ""
    context: SkillContext = SkillContext.INLINE
    allowed_tools: tuple[str, ...] = ()
    model: str = ""
    effort: str = ""
    argument_hint: str = ""
    disable_model_invocation: bool = False
    activation_patterns: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_capabilities(self) -> "SkillManifest":
        for value in (*self.allowed_tools, *self.activation_patterns):
            if not value or value != value.strip():
                raise ValueError("Skill capability and path declarations must be non-blank")
        if self.context is SkillContext.INLINE and (self.allowed_tools or self.model or self.effort):
            raise ValueError("inline Skill cannot declare fork-only capabilities")
        if any(re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", tool) is None for tool in self.allowed_tools):
            raise ValueError("Skill allowed tool identity is invalid")
        if self.model and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}", self.model) is None:
            raise ValueError("Skill model route identity is invalid")
        if self.effort and self.effort not in {"low", "medium", "high", "xhigh", "max"}:
            raise ValueError("Skill effort is unsupported")
        return self


class SkillSourceEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, arbitrary_types_allowed=True)

    canonical_path: Path
    content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    trust_decision: str = Field(pattern=r"^approved$")
    approval_generation: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_path(self) -> "SkillSourceEvidence":
        if not self.canonical_path.is_absolute():
            raise ValueError("Skill source path must be canonical and absolute")
        return self


class ActivatedSkillSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    manifest: SkillManifest
    source: SkillSourceEvidence
    instructions: str = Field(min_length=1)
    tool_binding_generation: int = Field(ge=1)
    tokenizer_identity: str = Field(min_length=1)
    token_cost: int = Field(ge=1)

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def description(self) -> str:
        return self.manifest.description

    @property
    def when_to_use(self) -> str:
        return self.manifest.when_to_use

    @property
    def context(self) -> str:
        return self.manifest.context.value

    @property
    def allowed_tools(self) -> tuple[str, ...]:
        return self.manifest.allowed_tools

    @property
    def model(self) -> str:
        return self.manifest.model

    @property
    def effort(self) -> str:
        return self.manifest.effort

    @property
    def argument_hint(self) -> str:
        return self.manifest.argument_hint

    @property
    def disable_model_invocation(self) -> bool:
        return self.manifest.disable_model_invocation

    @property
    def source_path(self) -> Path:
        return self.source.canonical_path

    @property
    def activation_patterns(self) -> tuple[str, ...]:
        return self.manifest.activation_patterns

    @property
    def is_conditional(self) -> bool:
        return bool(self.manifest.activation_patterns)


__all__ = ["ActivatedSkillSnapshot", "SkillContext", "SkillManifest", "SkillSourceEvidence"]
