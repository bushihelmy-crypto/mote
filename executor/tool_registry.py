"""
ToolRegistry — global registry for BaseTool subclasses.

Usage:
    from mote.executor.tool_registry import register_tool, registry

    @register_tool
    class MyTool(BaseTool):
        name = "MyTool"
        async def call(self, **kwargs): ...

    # Lookup (only ToolExecutor should do this)
    cls = registry.get("MyTool")

Tools themselves should not access the registry.
"""
from __future__ import annotations

import importlib
import pkgutil
from typing import ClassVar

from mote.common.base.singleton import Singleton


class ToolRegistry(metaclass=Singleton):
    """Singleton registry for BaseTool subclasses."""

    _FROZEN_METHODS: ClassVar[frozenset] = frozenset({"bind", "session_id"})
    _discovered: ClassVar[bool] = False

    def __init__(self):
        self._registry = {}

    # Packages scanned for @register_tool classes. Tools are expected to live in
    # one of these; a whitelist keeps discovery fast and side-effect-free instead
    # of importing the whole mote tree.
    _SCAN_PACKAGES: ClassVar[tuple[str, ...]] = ("mote.executor.tools",)

    def discover(self) -> None:
        """Recursively import modules under the whitelisted packages so each
        @register_tool runs.

        This is what makes the registry pattern self-contained: a tool registers
        simply by wearing @register_tool inside a scanned package — no central
        import list to maintain. Modules that fail to import (optional deps, etc.)
        are skipped. Idempotent; safe to call repeatedly.
        """
        if ToolRegistry._discovered:
            return
        ToolRegistry._discovered = True
        for package in self._SCAN_PACKAGES:
            try:
                pkg = importlib.import_module(package)
            except Exception:  # noqa: BLE001 — skip a package that can't be imported
                continue
            for mod in pkgutil.walk_packages(pkg.__path__, prefix=pkg.__name__ + "."):
                try:
                    importlib.import_module(mod.name)
                except Exception:  # noqa: BLE001 — best-effort scan; skip unimportable modules
                    continue

    def register(self, cls):
        """Class decorator that registers a BaseTool subclass.

        - name: uses cls.name if set, otherwise cls.__name__ (this is the lookup key)
        - aliases: registers additional lookup names if provided
        - Validates no frozen methods are overridden
        - Names/aliases must not collide with a different registered tool

        Registers under resolved name + all cls.aliases. Schema/description are
        the tool's own responsibility (BaseTool.get_schema) — not the registry's.
        """
        name = getattr(cls, "name", "") or cls.__name__
        cls.name = name

        for method in self._FROZEN_METHODS:
            if method in cls.__dict__:
                raise TypeError(f"@register_tool: class '{cls.__name__}' must not override '{method}'")

        self._check_conflict(name, cls)
        self._registry[name] = cls
        for alias in getattr(cls, "aliases", []):
            self._check_conflict(alias, cls)
            self._registry[alias] = cls

        return cls

    def _check_conflict(self, key: str, cls) -> None:
        """Reject a name/alias already taken by a *different* tool class.

        Re-registering the same class under the same key is idempotent (allowed),
        e.g. when discover() re-imports a module.
        """
        existing = self._registry.get(key)
        if existing is not None and existing is not cls:
            raise ValueError(
                f"@register_tool: name '{key}' already registered to "
                f"'{existing.__name__}', cannot reassign to '{cls.__name__}'."
            )

    def get(self, name: str) -> type | None:
        """Look up a registered tool class by name or alias."""
        return self._registry.get(name)

    def all_tools(self) -> dict[str, type]:
        """Return all registered tool classes (deduplicated, primary name only)."""
        seen = {}
        for name, tool_cls in self._registry.items():
            if tool_cls not in seen.values():
                seen[tool_cls.name] = tool_cls
        return seen

    def all_names(self, cls) -> list[str]:
        """Return all names a tool class responds to (primary + aliases)."""
        names = [cls.name] if cls.name else [cls.__name__]
        names.extend(getattr(cls, "aliases", []))
        return names


# Singleton instance
registry = ToolRegistry()

# Convenience alias — usage: @register_tool
register_tool = registry.register
