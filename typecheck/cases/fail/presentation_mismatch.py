"""Presentation projectors and consumers preserve their event domains."""

from mote.contracts.events.session import SessionStartEvent
from mote.product.presentation.projection.projector import ViewProjector

projector = ViewProjector()
projector.project(SessionStartEvent())
