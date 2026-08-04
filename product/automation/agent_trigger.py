"""Map generic automation triggers to agent commands."""

from mote.contracts.ports.agent.delivery import (
    AgentDeliveryCommand,
    AgentDeliveryCommandDisposition,
    AgentDeliveryPort,
    AgentDeliverySourceKind,
)
from mote.orchestration.automation import AutomationTrigger, TriggerDisposition, TriggerReceipt


class AgentTriggerAdapter:
    def __init__(self, control: AgentDeliveryPort, *, default_target: str = "") -> None:
        self._control = control
        self._default_target = default_target

    def dispatch(self, trigger: AutomationTrigger) -> TriggerReceipt:
        target = trigger.target or self._default_target
        if not target:
            return TriggerReceipt(TriggerDisposition.REJECTED, reason="missing target")
        try:
            receipt = self._control.dispatch(
                AgentDeliveryCommand(
                    AgentDeliverySourceKind.AUTOMATION,
                    trigger.trigger_id,
                    target,
                    trigger.content,
                )
            )
        except Exception as exc:  # noqa: BLE001
            return TriggerReceipt(TriggerDisposition.REJECTED, reason=str(exc))
        if receipt.disposition is AgentDeliveryCommandDisposition.REJECTED:
            return TriggerReceipt(TriggerDisposition.REJECTED, reason=receipt.reason)
        return TriggerReceipt(
            TriggerDisposition.ACCEPTED,
            receipt_id=receipt.delivery_id,
        )


__all__ = ["AgentTriggerAdapter"]
