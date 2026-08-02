"""Cross-boundary event-family guarantees; not an event payload union."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class EventFamily(StrEnum):
    CONTROL = "control"
    AUDIT = "audit"
    OBSERVATION = "observation"


class EventDurability(StrEnum):
    DOMAIN_DEFINED = "domain_defined"
    DURABLE_PROJECTION = "durable_projection"
    PROCESS_LOCAL = "process_local"


class EventDeliveryGuarantee(StrEnum):
    TYPED_RECEIPT = "typed_receipt"
    IDEMPOTENT_REPLAY = "idempotent_replay"
    BOUNDED_LOSS = "bounded_loss"


class ConsumerEffectPolicy(StrEnum):
    OWNER_COMMAND = "owner_command"
    APPEND_ONLY_AUDIT = "append_only_audit"
    NO_AUTHORITATIVE_MUTATION = "no_authoritative_mutation"


@dataclass(frozen=True, slots=True)
class EventFamilyPolicy:
    family: EventFamily
    authoritative_source: str
    durability: EventDurability
    delivery: EventDeliveryGuarantee
    consumer_effect: ConsumerEffectPolicy
    replay: str
    retention: str


EVENT_FAMILY_POLICIES = {
    EventFamily.CONTROL: EventFamilyPolicy(
        family=EventFamily.CONTROL,
        authoritative_source="bounded-context command owner",
        durability=EventDurability.DOMAIN_DEFINED,
        delivery=EventDeliveryGuarantee.TYPED_RECEIPT,
        consumer_effect=ConsumerEffectPolicy.OWNER_COMMAND,
        replay="stable command identity; domain-owned idempotency",
        retention="domain lifecycle and legal policy",
    ),
    EventFamily.AUDIT: EventFamilyPolicy(
        family=EventFamily.AUDIT,
        authoritative_source="projection from an already committed fact",
        durability=EventDurability.DURABLE_PROJECTION,
        delivery=EventDeliveryGuarantee.IDEMPOTENT_REPLAY,
        consumer_effect=ConsumerEffectPolicy.APPEND_ONLY_AUDIT,
        replay="idempotent by source fact identity and revision",
        retention="Product audit and legal policy",
    ),
    EventFamily.OBSERVATION: EventFamilyPolicy(
        family=EventFamily.OBSERVATION,
        authoritative_source="domain owner observation after local decision",
        durability=EventDurability.PROCESS_LOCAL,
        delivery=EventDeliveryGuarantee.BOUNDED_LOSS,
        consumer_effect=ConsumerEffectPolicy.NO_AUTHORITATIVE_MUTATION,
        replay="none; loss cannot affect reconciliation or control",
        retention="bounded process-local mailbox only",
    ),
}


__all__ = [
    "ConsumerEffectPolicy",
    "EVENT_FAMILY_POLICIES",
    "EventDeliveryGuarantee",
    "EventDurability",
    "EventFamily",
    "EventFamilyPolicy",
]
