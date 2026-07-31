"""Product presentation events, projection, state, and rendering."""

from mote.product.presentation.consumer import BaseConsumer, SinkConsumer
from mote.product.presentation.consumer_protocol import Consumer
from mote.product.presentation.projection.base import BaseProjector
from mote.product.presentation.projection.projector import ViewProjector

__all__ = [
    "BaseConsumer",
    "BaseProjector",
    "Consumer",
    "SinkConsumer",
    "ViewProjector",
]
