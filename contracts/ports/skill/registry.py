"""Narrow ports through which Runtime consumes Product-owned Skills."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, Sequence


class SkillDefinitionView(Protocol):
    name: str
    description: str
    instructions: str
    context: str
    allowed_tools: tuple[str, ...]
    model: str
    effort: str
    argument_hint: str
    disable_model_invocation: bool
    source_path: Path
    tool_binding_generation: int
    tokenizer_identity: str
    token_cost: int

    @property
    def is_conditional(self) -> bool: ...


class SkillCatalog(Protocol):
    def get(self, name: str) -> SkillDefinitionView | None: ...

    def get_all(self) -> list[SkillDefinitionView]: ...

    def get_skill_count(self) -> int: ...


class SkillPromptProvider(Protocol):
    def build_index(self, max_tokens: int = 2000, only_names: set | None = None) -> str: ...


class SkillService(Protocol):
    @property
    def ready(self) -> bool: ...

    @property
    def enabled(self) -> bool: ...

    @property
    def pool(self) -> SkillCatalog | None: ...

    @property
    def injector(self) -> SkillPromptProvider | None: ...

    def ensure_ready(self) -> None: ...

    def reload(self) -> bool: ...

    def source_dirs(self) -> list[str]: ...


class SkillServiceFactory(Protocol):
    def build(self, *, skills: Sequence[str], config: Any, cwd: str) -> SkillService: ...


__all__ = [
    "SkillCatalog",
    "SkillDefinitionView",
    "SkillPromptProvider",
    "SkillService",
    "SkillServiceFactory",
]
