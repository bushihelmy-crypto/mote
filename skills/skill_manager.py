"""SkillManager — Skills subsystem lifecycle management.

Extracted from Role to decouple skill init/deploy/inject from the core
role class.  Role holds a lazy ``skill_manager`` property that delegates
all skill-related bookkeeping here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from metagpt.common.const import ATOMS_DIR_NAME, DEFAULT_WORKSPACE_ROOT
from metagpt.common.logs import logger
from metagpt.skills.skill_deployer import SkillDeployer
from metagpt.skills.skill_injector import SkillInjector
from metagpt.skills.skill_pool import SkillPool


class SkillManager:
    """Skills subsystem lifecycle management.

    Mirrors ToolExecutor's pattern: takes a declarative list of skill names,
    empty list = disabled, non-empty = load and deploy those skills.
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
        """Idempotent init — load, deploy, and prepare injector."""
        if self._ready:
            return

        if not self._skills:
            self._ready = True
            return

        self.pool = SkillPool()
        self.pool.load_by_names(self._skills)

        workspace = Path(DEFAULT_WORKSPACE_ROOT)
        if not workspace.exists():
            logger.warning(f"Skills: workspace {workspace} does not exist, skipping deployment")
            self._ready = True
            return

        atoms_dir = workspace / ATOMS_DIR_NAME
        index_path = atoms_dir / "SKILLS.md"
        skills = self.pool.get_all()

        needs_deploy = not index_path.exists() or any(
            not (atoms_dir / "skills" / s.name).exists() for s in skills
        )
        if needs_deploy:
            deployer = SkillDeployer()
            deployer.deploy(workspace=workspace, skills=skills)
            deployer.generate_index(workspace=workspace, skills=skills)

        self.injector = SkillInjector(
            pool=self.pool,
            skills_md_path=index_path,
        )

        logger.info(
            f"Skills subsystem initialized ({self.pool.get_skill_count()} skills)"
        )
        self._ready = True
