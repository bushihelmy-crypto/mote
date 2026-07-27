"""Per-language :class:`~mote.runtime.context.code_map.providers.base.ModuleResolver`s.

Each module here holds one language's stateless module ⇄ file arithmetic (the
name↔path math a provider's :meth:`module_resolver` hands back). Python is the
only resolver until the tree-sitter languages are wired.
"""

from __future__ import annotations

from mote.runtime.context.code_map.providers.resolvers.python import PythonModuleResolver

__all__ = ["PythonModuleResolver"]
