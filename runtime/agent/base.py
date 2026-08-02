#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Nominal Runtime Role lifecycle and Residency state boundary."""

from collections.abc import Mapping
from typing import TYPE_CHECKING, ClassVar

from mote.contracts.content import ContentDigest
from mote.contracts.conversation import Message
from mote.contracts.events.envelope import JsonValue
from mote.contracts.task.lifecycle import BackgroundTaskPinSnapshot

if TYPE_CHECKING:
    from mote.runtime.agent.incarnation import AgentIncarnationBlueprint


class BaseRole:
    """Base class for all roles.

    Subclasses must implement: think, act, react, run, get_memories, is_idle.
    """

    role_type_id: ClassVar[str | None] = None

    @property
    def session_id(self) -> str:
        raise NotImplementedError

    @property
    def residency_definition_id(self) -> str:
        raise NotImplementedError

    @property
    def residency_config_digest(self) -> ContentDigest:
        raise NotImplementedError

    def export_residency_state(self, *, session_history_is_durable: bool) -> Mapping[str, JsonValue]:
        raise NotImplementedError

    def restore_residency_message_buffer(self, snapshot: JsonValue) -> None:
        raise NotImplementedError

    def restore_residency_history(
        self,
        messages: tuple[Message, ...],
        session_meta: Mapping[str, object],
    ) -> None:
        raise NotImplementedError

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

    async def prepare_for_eviction(self) -> BackgroundTaskPinSnapshot | None:
        """Close incarnation resources while transferring shared ownership."""

        raise NotImplementedError(f"{type(self).__name__}.prepare_for_eviction() not implemented")
