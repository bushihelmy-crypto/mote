from __future__ import annotations

import pytest
from pydantic import ValidationError

from mote.contracts.config.model.routing import AgentRouterConfig, RouterConfig, SemanticRouteConfig
from mote.product.config.model.inputs import (
    ExplicitModelsConfig,
    ProductEndpointInput,
    ProductExplicitEndpointInput,
    ProductFailoverGroupInput,
    ProductRecoveryInput,
    ProductRoutesInput,
    ShortcutModelsConfig,
)
from mote.product.config.schema import Config


def _models():
    return ExplicitModelsConfig(
        mode="explicit",
        endpoints={
            "endpoint": ProductExplicitEndpointInput(
                provider="openai",
                model="gpt-4o",
                api_key="test",
            )
        },
        failover_groups={"group": ProductFailoverGroupInput(endpoints=["endpoint"], recovery_profile="default")},
        routes=ProductRoutesInput(
            default="group",
            semantic={
                "low": "group",
                "standard": "group",
                "strong": "group",
                "max": "group",
            },
        ),
        recovery_profiles={"default": ProductRecoveryInput()},
    )


def _shortcut():
    return ShortcutModelsConfig(default=ProductEndpointInput(provider="openai", model="gpt-4o", api_key="x"))


def _agent():
    return AgentRouterConfig(
        strategy="squilla",
        default_route="standard",
        candidates=("low", "standard", "strong", "max"),
        class_routes={
            "R0": "low",
            "R1": "standard",
            "R2": "strong",
            "R3": "max",
        },
    )


def test_semantic_pool_and_r0_r3_mapping_validate_at_activation():
    routes = {
        "low": SemanticRouteConfig(quality_class="R0", quality_rank=0),
        "standard": SemanticRouteConfig(quality_class="R1", quality_rank=1),
        "strong": SemanticRouteConfig(quality_class="R2", quality_rank=2),
        "max": SemanticRouteConfig(quality_class="R3", quality_rank=3),
    }
    config = Config(
        models=_models(),
        router=RouterConfig(main_agent=_agent(), routes=routes),
    )
    assert config.router.main_agent.class_routes["R3"] == "max"


def test_partial_squilla_class_mapping_is_rejected():
    with pytest.raises(ValidationError):
        AgentRouterConfig(
            strategy="squilla",
            class_routes={"R0": "low"},
        )


def test_task_or_unknown_route_cannot_enter_semantic_pool():
    routes = {
        "low": SemanticRouteConfig(quality_class="R0", quality_rank=0),
        "standard": SemanticRouteConfig(quality_class="R1", quality_rank=1),
        "strong": SemanticRouteConfig(quality_class="R2", quality_rank=2),
        "max": SemanticRouteConfig(quality_class="R3", quality_rank=3),
    }
    agent = _agent().model_copy(update={"candidates": (*_agent().candidates, "summary")})
    with pytest.raises(ValidationError, match="unknown semantic routes"):
        Config(
            models=_models(),
            router=RouterConfig(main_agent=agent, routes=routes),
        )


def test_task_and_semantic_route_names_are_disjoint():
    values = _models().model_dump()
    values["routes"]["tasks"] = {"low": "group"}
    with pytest.raises(ValidationError, match="must be disjoint"):
        ExplicitModelsConfig.model_validate(values)


def test_enabled_router_requires_a_bound_semantic_pool():
    with pytest.raises(ValidationError, match="empty semantic pool"):
        Config(
            models=_shortcut(),
            router=RouterConfig(main_agent=AgentRouterConfig(strategy="rule")),
        )


def test_semantic_metadata_requires_gateway_route_binding():
    routes = {"standard": SemanticRouteConfig()}
    with pytest.raises(ValidationError, match="no models.routes.semantic binding"):
        Config(
            models=_shortcut(),
            router=RouterConfig(routes=routes),
        )


def test_spawn_routing_requires_squilla_sub_agent():
    with pytest.raises(ValidationError, match="sub_agent.strategy='squilla'"):
        Config(
            models=_shortcut(),
            router=RouterConfig(spawn_routing=True),
        )
