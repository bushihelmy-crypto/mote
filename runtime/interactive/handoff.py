"""Agent-to-human ownership transfer for managed interactive runtimes."""
from __future__ import annotations

from mote.contracts.interaction.handoff import (
    DriverHandoffResult,
    HandoffOutcome,
    HandoffRequest,
    HandoffStatus,
    HumanHandoffOutcome,
)
from mote.contracts.ports.interaction.human import HumanInteractionPort
from mote.contracts.ports.runtime.driver import HandoffRuntimeDriver, LiveSurfaceRuntimeDriver
from mote.contracts.runtime import RuntimeCheckpoint
from mote.contracts.runtime.errors import ManagedRuntimeStateError
from mote.runtime.interactive.host import RuntimeHost
from mote.runtime.interactive.surface import RuntimeLiveSurfaceSession


class HandoffCoordinator:
    """Run one fenced Runtime handoff through a host-native interaction port."""

    def __init__(self, host: RuntimeHost, interaction: HumanInteractionPort) -> None:
        self._host = host
        self._interaction = interaction

    async def handoff(
        self,
        request: HandoffRequest,
        *,
        owner_id: str,
        expected_revision: int | None = None,
    ) -> HandoffOutcome:
        from_revision = self._host.descriptor(request.runtime_ref).revision
        human = HumanHandoffOutcome(status=HandoffStatus.FAILED)
        driver_result = DriverHandoffResult()

        async with self._host.handoff_access(
            request,
            owner_id=owner_id,
            expected_revision=expected_revision,
        ) as access:
            driver = access.driver
            if not isinstance(driver, HandoffRuntimeDriver):
                raise ManagedRuntimeStateError(
                    "runtime driver does not implement ownership handoff",
                    runtime_id=request.runtime_ref.runtime_id,
                )

            before = await self._checkpoint_if_available(access, "handoff-before")
            await access.prepare(before)
            handle = await driver.prepare_handoff(request)
            surface = None
            try:
                if handle.surface.kind not in driver.capabilities.surface_kinds:
                    raise ManagedRuntimeStateError(
                        "runtime driver returned an undeclared surface kind",
                        runtime_id=request.runtime_ref.runtime_id,
                        surface_kind=handle.surface.kind,
                    )
                surface = (
                    RuntimeLiveSurfaceSession(driver, handle) if isinstance(driver, LiveSurfaceRuntimeDriver) else None
                )
                await access.activate()
                human = await self._interaction.open_handoff(request, handle, surface)
            except BaseException:
                if surface is not None:
                    await surface.aclose()
                await driver.finish_handoff(handle, human)
                raise
            driver_result = await driver.finish_handoff(handle, human)
            after = await self._checkpoint_if_available(access, "handoff-after")

            changed = human.status is HandoffStatus.COMPLETED or self._checkpoint_changed(before, after)
            access.commit(changed=changed, outcome=human, checkpoint=after)

        return HandoffOutcome(
            status=human.status,
            runtime_ref=request.runtime_ref,
            from_revision=from_revision,
            to_revision=self._host.descriptor(request.runtime_ref).revision,
            human_message=human.human_message,
            detail=human.detail,
            summary=driver_result.summary,
            resume_hint=driver_result.resume_hint,
        )

    @staticmethod
    async def _checkpoint_if_available(access, reason: str) -> RuntimeCheckpoint | None:
        try:
            return await access.checkpoint(reason)
        except RuntimeError:
            # A live foreground program may have no coherent logical snapshot;
            # exclusive fencing still makes the ownership transfer safe.
            return None

    @staticmethod
    def _checkpoint_changed(before: RuntimeCheckpoint | None, after: RuntimeCheckpoint | None) -> bool:
        if before is None or after is None:
            return False
        if before.digest and after.digest:
            return before.digest != after.digest
        return before.payload_ref != after.payload_ref


__all__ = ["HandoffCoordinator"]
