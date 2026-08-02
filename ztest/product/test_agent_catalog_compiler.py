from __future__ import annotations

from dataclasses import dataclass

import pytest

from mote.contracts.agent import AgentConstructionRequest, BaseAgent, SpawnableAgentDefinition
from mote.product.agents.catalog import AgentCatalog, compile_agent_catalog
from mote.product.agents.discovery import builtin_agent_catalog
from mote.product.agents.factory import CodingAgentFactory
from mote.product.extensions.sources import ExtensionSourcePolicy
from mote.runtime.agent.base import BaseRole


@dataclass(frozen=True, slots=True)
class _Builder:
    identity: str

    def build(self, request: AgentConstructionRequest):
        raise AssertionError(f"catalog test builder {self.identity} must not execute: {request}")


def _definition(
    name: str,
    *,
    aliases: tuple[str, ...] = (),
    description: str = "description",
    identity: str = "definition-v1",
) -> SpawnableAgentDefinition[str]:
    return SpawnableAgentDefinition(
        name=name,
        aliases=aliases,
        description=description,
        version=identity,
        builder=_Builder(name),
    )


@pytest.mark.parametrize(
    ("left", "right"),
    (
        (_definition("alpha"), _definition("alpha")),
        (_definition("alpha", aliases=("shared",)), _definition("beta", aliases=("shared",))),
        (_definition("alpha", aliases=("beta",)), _definition("beta")),
        (_definition("alpha"), _definition("beta", aliases=("alpha",))),
    ),
)
def test_complete_namespace_conflicts_fail_closed(left, right):
    with pytest.raises(ValueError, match="belongs to both"):
        compile_agent_catalog((left, right))


def test_version_and_lookup_are_independent_of_declaration_order():
    alpha = _definition("alpha", aliases=("z", "a"))
    beta = _definition("beta")

    forward = compile_agent_catalog((alpha, beta))
    reverse = compile_agent_catalog((beta, alpha))

    assert forward.version == reverse.version
    assert forward.get("alpha") is alpha
    assert forward.get("a") is alpha
    assert reverse.get("a") is alpha


@pytest.mark.parametrize(
    "changed",
    (
        _definition("other"),
        _definition("alpha", aliases=("alias",)),
        _definition("alpha", description="other"),
        _definition("alpha", identity="definition-v2"),
    ),
)
def test_every_canonical_identity_field_changes_snapshot_version(changed):
    baseline = compile_agent_catalog((_definition("alpha"),))
    assert compile_agent_catalog((changed,)).version != baseline.version


class _Alpha(BaseAgent, BaseRole):
    agent_name = "alpha"
    aliases = ["a"]
    description = "alpha definition"
    definition_version = "alpha-v1"


class _Beta(BaseAgent, BaseRole):
    agent_name = "beta"
    aliases = ["b"]
    description = "beta definition"
    definition_version = "beta-v1"


def test_full_and_incremental_type_projection_use_the_same_compiler():
    factory = CodingAgentFactory()
    full = AgentCatalog.from_types((_Beta, _Alpha), factory)
    incremental = AgentCatalog.from_types((_Alpha,), factory).with_types((_Beta,), factory)

    assert full.version == incremental.version
    assert tuple(full.all_agents()) == ("alpha", "beta")


def test_builtin_discovery_is_a_thin_projection_to_the_same_compiler(monkeypatch, tmp_path):
    factory = CodingAgentFactory()
    monkeypatch.setattr(
        "mote.product.agents.discovery.discover_md_agents",
        lambda cwd, *, source_policy: {"beta": _Beta, "alpha": _Alpha},
    )

    discovered = builtin_agent_catalog(
        factory,
        tmp_path,
        source_policy=ExtensionSourcePolicy(user_root=tmp_path, builtin_roots=()),
    )
    direct = AgentCatalog.from_types((_Alpha, _Beta), factory)

    assert discovered.version == direct.version


def test_direct_catalog_construction_is_forbidden():
    with pytest.raises(TypeError, match="compile_agent_catalog"):
        AgentCatalog(version="invented")
