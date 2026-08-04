from __future__ import annotations

from pathlib import Path

import pytest

from mote.kernel.output import text_output_contract
from mote.product.agents.markdown_loader import _build_agent_class
from mote.product.config.model_checkpoint import approved_model_checkpoint_policy
from mote.product.extensions.sources import ExtensionKind, ExtensionSourcePolicy
from mote.runtime.agent import AgentDependencies, AgentWiring, RoleSchema
from mote.runtime.agent.session_manager import SessionResumeIdentityError


def _source(path: Path):
    policy = ExtensionSourcePolicy(user_root=path.parents[1], builtin_roots=())
    source = policy.inspect(ExtensionKind.AGENT, path)
    assert source.approved
    return source


def _definition(path: Path, *, body: str = "Review carefully."):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\nname: reviewer\ndescription: Review code\n---\n" + body,
        encoding="utf-8",
    )
    cls = _build_agent_class(
        "reviewer",
        {"name": "reviewer", "description": "Review code"},
        body,
        source=_source(path),
    )
    assert cls is not None
    return cls


def _wiring():
    from mote.product.agents.factory import CodingAgentFactory

    return AgentWiring.for_dependencies(
        CodingAgentFactory(
            model_checkpoint_policy=approved_model_checkpoint_policy(),
        ).dependencies(deps=None, output_contract=text_output_contract())
    )


def test_definition_identity_binds_approved_source_path_and_content(tmp_path: Path) -> None:
    first_path = tmp_path / "user" / "agents" / "reviewer.md"
    first = _definition(first_path)
    changed = _definition(first_path, body="Review changed instructions.")
    copied = _definition(tmp_path / "user" / "other" / "reviewer.md")

    assert first.definition_id.startswith("mote.agent.markdown.v1.sha256-")
    assert first.definition_version == first.definition_id
    assert first.definition_id != changed.definition_id
    assert first.definition_id != copied.definition_id
    assert first.definition_source_digest != changed.definition_source_digest


def test_same_name_definitions_are_application_scoped_not_global_registry(
    tmp_path: Path,
) -> None:
    first = _definition(tmp_path / "user" / "project-a" / "reviewer.md")
    second = _definition(tmp_path / "user" / "project-b" / "reviewer.md")

    assert first.agent_name == second.agent_name == "reviewer"
    assert first is not second
    assert first.definition_id != second.definition_id


def test_markdown_residency_round_trip_preserves_schema_state_and_definition(
    tmp_path: Path,
) -> None:
    definition = _definition(tmp_path / "user" / "agents" / "reviewer.md")
    original = definition(parent_session_id="parent-1", wiring=_wiring())
    original.state.working_dir = "/workspace/restored"
    blueprint = original.incarnation_blueprint()

    restored = blueprint.build(original.export_residency_state(session_history_is_durable=False))

    assert restored.session_id == original.session_id
    assert restored.state.parent_session_id == "parent-1"
    assert restored.state.working_dir == "/workspace/restored"
    assert restored.role_schema == original.role_schema
    assert restored.residency_definition_id == definition.definition_id


def test_definition_change_rejects_old_session_and_schema_substitution(
    tmp_path: Path,
) -> None:
    path = tmp_path / "user" / "agents" / "reviewer.md"
    old_definition = _definition(path)
    old = old_definition(wiring=_wiring())
    new_definition = _definition(path, body="New approved behavior.")
    current = new_definition(wiring=_wiring())
    meta = {
        "role_class": old.residency_definition_id,
        "toolset_manifest": [],
    }

    with pytest.raises(SessionResumeIdentityError, match="same role class"):
        current.validate_resume_identity(meta)
    with pytest.raises(ValueError, match="does not match approved source"):
        new_definition(
            wiring=_wiring(),
            role_schema=RoleSchema(name="substituted", instruction="attacker"),
        )
