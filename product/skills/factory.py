"""Product composition adapter for the Skills subsystem."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from mote.contracts.ports.skill.registry import SkillService
from mote.product.paths import mote_project_dirs, user_mote_dir
from mote.product.skills.skill_manager import SkillManager
from mote.product.skills.skill_pool import _BUILTIN_DIR


class ProductSkillServiceFactory:
    def __init__(self, user_config_root: Path | None = None) -> None:
        self._user_config_root = user_config_root

    def build(self, *, skills: Sequence[str], config: Any, cwd: str) -> SkillService:
        dirs = [_BUILTIN_DIR]
        if config.include_user_dir:
            dirs.append(user_mote_dir("skills", user_config_root=self._user_config_root))
        if config.include_project_dir:
            dirs.extend(mote_project_dirs("skills", Path(cwd)))
        dirs.extend(Path(path) for path in config.extra_dirs)
        return SkillManager(skills=list(skills), enabled=config.enabled, source_dirs=dirs)


__all__ = ["ProductSkillServiceFactory"]
