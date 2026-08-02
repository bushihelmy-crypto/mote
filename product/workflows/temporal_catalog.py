"""Product-owned explicit catalog for the optional Temporal effect plane."""

from __future__ import annotations

from importlib import import_module

from mote.contracts.config.tool import TemporalConfig
from mote.product.workflows.durability import TemporalEffectPlane
from mote.runtime.session.workspace.store import SessionWorkspace


def activate_temporal_effect_plane(
    config: TemporalConfig,
    *,
    workspace: SessionWorkspace,
    dispatch,
) -> TemporalEffectPlane:
    module = import_module("mote.product.workflows.temporal_effects")
    plane_type = module.TemporalWorkflowEffects
    return plane_type(config, workspace=workspace, dispatch=dispatch)


__all__ = ["activate_temporal_effect_plane"]
