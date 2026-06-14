"""SkillManager — Skills subsystem lifecycle management.

Extracted from Role to decouple skill init/inject from the core role class.
Role holds a lazy ``skill_manager`` property that delegates all skill-related
bookkeeping here.
"""

from __future__ import annotations

from typing import Optional

from metagpt.common.logs import log_class
from metagpt.skills.skill_injector import SkillInjector
from metagpt.skills.skill_pool import SkillPool


@log_class(level="DEBUG")
class SkillManager:
    """Skills subsystem lifecycle management.

    Mirrors ToolExecutor's pattern: takes a declarative list of skill names,
    empty list = disabled, non-empty = load those skills and prepare the
    injector. Skills are read directly from the builtin directory; nothing is
    copied to disk.
    """

    def __init__(self, skills: list[str]):
        self._skills = skills
        self.pool: Optional[SkillPool] = None
        self.injector: Optional[SkillInjector] = None
        self._ready = False

    @property
    def ready(self) -> bool:
        return self._ready

    def ensure_ready(self):
        """Idempotent init — load skills and prepare the injector."""
        if self._ready:
            return

        if not self._skills:
            self._ready = True
            return

        self.pool = SkillPool()
        self.pool.load_by_names(self._skills)

        self.injector = SkillInjector(pool=self.pool)

        self._ready = True
