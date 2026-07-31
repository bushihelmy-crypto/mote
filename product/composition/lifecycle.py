"""Lifecycle projection for resources owned by Product composition."""

from __future__ import annotations

from mote.product.routing.squilla.ml.runtime import RoutingModelRuntime
from mote.runtime.control.lifecycle import LifecycleResource


def lifecycle_resources(
    routing_models: RoutingModelRuntime,
) -> tuple[LifecycleResource, ...]:
    return (routing_models.lifecycle_resource(),)


__all__ = ["lifecycle_resources"]
