#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
BaseRole — persistent polymorphic base for Runtime roles.
Provides the polymorphic registry for serialization. No ABC, no Pydantic.
"""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from mote.runtime.agent.incarnation import AgentIncarnationBlueprint

# ============================================================================
# Polymorphic registry for serialization/deserialization
# ============================================================================

_ROLE_REGISTRY: dict[str, type["BaseRole"]] = {}
_LEGACY_ROLE_REGISTRY: dict[str, type["BaseRole"]] = {}


def _qualified_name(cls: type) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


class BaseRole:
    """Base class for all roles.

    Provides:
      - Automatic subclass registration for polymorphic deserialization
      - dump()/load() serialization protocol

    Subclasses must implement: think, act, react, run, get_memories, is_idle.
    """

    role_type_id: ClassVar[str | None] = None
    legacy_role_type_ids: ClassVar[tuple[str, ...]] = ()
    replace_role_type_registration: ClassVar[bool] = False

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # Only an ID declared on this exact subclass registers it.  Inheriting a
        # parent's ID would make every test/helper subclass silently replace the
        # persisted type owner.
        type_id = cls.__dict__.get("role_type_id")
        if type_id:
            existing = _ROLE_REGISTRY.get(type_id)
            replace = bool(cls.__dict__.get("replace_role_type_registration", False))
            if existing is not None and existing is not cls and not replace:
                raise TypeError(f"duplicate role type id {type_id!r}: {existing} and {cls}")
            _ROLE_REGISTRY[type_id] = cls
        for legacy_id in cls.__dict__.get("legacy_role_type_ids", ()):
            _LEGACY_ROLE_REGISTRY[legacy_id] = cls

    # =========================================================================
    # Serialization protocol
    # =========================================================================

    def dump(self) -> dict[str, Any]:
        """Serialize role to a dict. Subclasses should override."""
        raise NotImplementedError(f"{type(self).__name__}.dump() not implemented")

    def validate_resume_identity(self, meta: Mapping[str, object]) -> None:
        """Validate durable session metadata before restoring any state.

        Persistence-capable subclasses must implement this fail-closed boundary.
        Keeping it on the nominal base prevents orchestration rehydrate paths
        from bypassing the Runtime's normal resume identity checks.
        """

        raise NotImplementedError(f"{type(self).__name__}.validate_resume_identity() not implemented")

    def incarnation_blueprint(self) -> "AgentIncarnationBlueprint":
        """Return the in-process construction recipe used by Residency."""

        raise NotImplementedError(f"{type(self).__name__}.incarnation_blueprint() not implemented")

    async def prepare_for_eviction(self) -> None:
        """Close incarnation resources while transferring shared ownership."""

        raise NotImplementedError(f"{type(self).__name__}.prepare_for_eviction() not implemented")

    @classmethod
    def load(cls, data: dict[str, Any]) -> "BaseRole":
        """Deserialize a role from a dict. Uses the polymorphic registry."""
        type_id = data.get("type_id")
        legacy_id = data.get("__module_class_name") if type_id is None else None
        if type_id is None and legacy_id is None:
            raise ValueError("Missing type_id in serialized role data")
        klass = _ROLE_REGISTRY.get(type_id) if type_id is not None else _LEGACY_ROLE_REGISTRY.get(legacy_id)
        if klass is None:
            unknown = type_id if type_id is not None else legacy_id
            raise TypeError(f"Unknown role type: {unknown}. Has it been registered?")
        return klass._from_dict(data)

    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> "BaseRole":
        """Reconstruct this specific class from serialized data. Override in subclasses."""
        raise NotImplementedError(f"{cls.__name__}._from_dict() not implemented")
