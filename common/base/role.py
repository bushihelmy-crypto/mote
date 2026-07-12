#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
BaseRole — minimal base class for all roles.
Provides the polymorphic registry for serialization. No ABC, no Pydantic.
"""

from typing import Any

# ============================================================================
# Polymorphic registry for serialization/deserialization
# ============================================================================

_ROLE_REGISTRY: dict[str, type["BaseRole"]] = {}


def _qualified_name(cls: type) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


class BaseRole:
    """Base class for all roles.

    Provides:
      - Automatic subclass registration for polymorphic deserialization
      - dump()/load() serialization protocol

    Subclasses must implement: think, act, react, run, get_memories, is_idle.
    """

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        _ROLE_REGISTRY[_qualified_name(cls)] = cls

    # =========================================================================
    # Serialization protocol
    # =========================================================================

    def dump(self) -> dict[str, Any]:
        """Serialize role to a dict. Subclasses should override."""
        raise NotImplementedError(f"{type(self).__name__}.dump() not implemented")

    @classmethod
    def load(cls, data: dict[str, Any]) -> "BaseRole":
        """Deserialize a role from a dict. Uses the polymorphic registry."""
        class_name = data.get("__module_class_name")
        if not class_name:
            raise ValueError("Missing __module_class_name in serialized role data")
        klass = _ROLE_REGISTRY.get(class_name)
        if klass is None:
            raise TypeError(f"Unknown role class: {class_name}. Has it been imported?")
        return klass._from_dict(data)

    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> "BaseRole":
        """Reconstruct this specific class from serialized data. Override in subclasses."""
        raise NotImplementedError(f"{cls.__name__}._from_dict() not implemented")
