"""SkillManager — Product-owned Skills lifecycle management.

Decouples skill init/inject from the core role class. Role holds a lazy
``skill_manager`` property that delegates all skill-related bookkeeping here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from mote.product.extensions.sources import ExtensionSourcePolicy
from mote.product.skills.skill_injector import SkillInjector
from mote.product.skills.skill_pool import SkillPool
from mote.runtime.telemetry.logging import log_class


@log_class(level="DEBUG")
class SkillManager:
    """Skills subsystem lifecycle management.

    Takes a declarative include filter (``skills``) plus an ``enabled`` master
    switch and an optional list of layered ``source_dirs``:

    - ``enabled`` gates the whole subsystem. When ``None`` (the default) it
      is inferred from a non-empty ``skills`` list, so an empty list means
      "disabled".
    - When enabled, skills are auto-discovered across all ``source_dirs``;
      a non-empty ``skills`` list narrows that to an include filter (empty list
      = load everything discovered).

    Skills are read directly from disk; nothing is copied.
    """

    def __init__(
        self,
        skills: list[str],
        *,
        enabled: Optional[bool] = None,
        source_dirs: Optional[list[Path]] = None,
        source_policy: ExtensionSourcePolicy,
    ):
        self._skills = skills
        # Inference: with no explicit master switch, a non-empty skills
        # list means "enabled" (and an empty one "disabled").
        self._enabled = bool(skills) if enabled is None else enabled
        self._source_dirs = source_dirs
        self._source_policy = source_policy
        self.pool: Optional[SkillPool] = None
        self.injector: Optional[SkillInjector] = None
        self._ready = False

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def enabled(self) -> bool:
        """Whether the skills subsystem is engaged for this role.

        The single source of truth for the "skills on?" decision (the global
        master switch OR the per-role opt-in inferred from a non-empty include
        list), read by the executor to decide whether to expose the ``Skill``
        bridge tool.
        """
        return self._enabled

    def _new_pool(self) -> SkillPool:
        """Build a pool over the configured source directories (or the default)."""
        if self._source_dirs is not None:
            return SkillPool(source_dirs=self._source_dirs, source_policy=self._source_policy)
        return SkillPool(source_policy=self._source_policy)

    def _load(self, pool: SkillPool) -> None:
        """Load skills into ``pool`` — by name when filtered, else everything."""
        if self._skills:
            pool.load_by_names(self._skills)
        else:
            pool.load_all()

    def ensure_ready(self):
        """Idempotent init — load skills and prepare the injector."""
        if self._ready:
            return

        if not self._enabled:
            self._ready = True
            return

        pool = self._new_pool()
        self._load(pool)
        self.pool = pool
        self.injector = SkillInjector(pool=pool)

        self._ready = True

    def reload(self) -> bool:
        """Re-scan skills from disk and atomically swap the pool + injector.

        No-op (returns ``False``) until :meth:`ensure_ready` has run or when the
        subsystem is disabled. A fresh pool is loaded fully *before* the swap,
        and the swap itself is a single synchronous assignment — so a concurrent
        ``think()`` cycle reading ``injector.build_content()`` (which reads the
        pool live) never observes a half-cleared pool. Returns ``True`` when a
        reload actually happened.
        """
        if not self._ready or not self._enabled:
            return False

        pool = self._new_pool()
        self._load(pool)
        self.pool = pool
        self.injector = SkillInjector(pool=pool)
        return True

    def source_dirs(self) -> list[str]:
        """Filesystem dirs to watch for skill hot-reload.

        Reports every layered source directory (resolved by :class:`SkillPool`)
        so the file watcher covers user/project skill locations too. Reads the
        live pool when ready so test-time redirection of the builtin location is
        honoured.
        """
        pool = self.pool or self._new_pool()
        return [str(d) for d in pool.source_dirs]
