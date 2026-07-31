"""Map generic automation triggers to agent commands."""

import uuid

from mote.orchestration.agents.control import AgentControl
from mote.orchestration.automation import AutomationTrigger, TriggerDisposition, TriggerReceipt


class AgentTriggerAdapter:
    def __init__(self, control: AgentControl, *, default_target: str = "") -> None:
        self._control = control
        self._default_target = default_target

    def dispatch(self, trigger: AutomationTrigger) -> TriggerReceipt:
        target = trigger.target or self._default_target
        if not target:
            return TriggerReceipt(TriggerDisposition.REJECTED, reason="missing target")
        runtimes = self._control.runtimes()
        runtime = runtimes.get(target)
        if runtime is not None and getattr(runtime, "active_turn", False):
            return TriggerReceipt(TriggerDisposition.DEFERRED, reason="target active")
        try:
            self._control.dispatch_automation(target, trigger.content)
        except Exception as exc:  # noqa: BLE001
            return TriggerReceipt(TriggerDisposition.REJECTED, reason=str(exc))
        return TriggerReceipt(
            TriggerDisposition.ACCEPTED,
            receipt_id=uuid.uuid4().hex,
        )


__all__ = ["AgentTriggerAdapter"]
