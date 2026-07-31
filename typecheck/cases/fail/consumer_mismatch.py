"""Consumers reject events outside their declared domain."""

from mote.product.presentation.consumer_protocol import Consumer


class EventA:
    pass


class EventB:
    pass


async def deliver(consumer: Consumer[EventA], event: EventB) -> None:
    await consumer.handle(event)
