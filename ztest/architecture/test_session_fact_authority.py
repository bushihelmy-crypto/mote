from __future__ import annotations

import ast
from pathlib import Path
from typing import get_args

import pytest

from mote.contracts.ports.session.facts import RolloutSourceEvent, SessionFactSink
from mote.runtime.session.codec import SESSION_ACTIVE_CODECS, STABLE_SESSION_EVENT_CLASSES
from mote.runtime.session.event_policy import ROLLOUT_EVENT_TYPES
from mote.runtime.session.events import SESSION_EVENT_CLASSES, SessionEvent

ROOT = Path(__file__).resolve().parents[2]


def test_session_union_discriminator_and_codec_catalog_are_identical() -> None:
    union_classes = frozenset(get_args(SessionEvent))
    discriminator_classes = frozenset(SESSION_EVENT_CLASSES.values())
    stable_classes = frozenset(STABLE_SESSION_EVENT_CLASSES.values())
    codec_event_types = {entry.event_type for entry in SESSION_ACTIVE_CODECS}

    assert union_classes == discriminator_classes == stable_classes
    assert codec_event_types == set(STABLE_SESSION_EVENT_CLASSES)
    assert len(SESSION_ACTIVE_CODECS) == len(SESSION_EVENT_CLASSES)


def test_session_discriminator_authority_is_immutable() -> None:
    event_type, event_class = next(iter(SESSION_EVENT_CLASSES.items()))
    with pytest.raises(TypeError):
        SESSION_EVENT_CLASSES[event_type] = event_class  # type: ignore[index]
    assert frozenset(SESSION_EVENT_CLASSES.values()) == frozenset(STABLE_SESSION_EVENT_CLASSES.values())


def test_rollout_policy_is_derived_from_closed_source_union() -> None:
    assert ROLLOUT_EVENT_TYPES == frozenset(get_args(RolloutSourceEvent))
    assert len(ROLLOUT_EVENT_TYPES) == 12


def test_session_fact_sink_does_not_accept_object() -> None:
    annotation = SessionFactSink.commit_fact.__annotations__["event"]
    assert annotation != object
    assert "RolloutSourceEvent" in str(annotation)


def test_committer_has_no_object_admission_or_cast_recovery() -> None:
    path = ROOT / "runtime/session/committer.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    commit_fact = next(
        node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "commit_fact"
    )
    assert "object" not in ast.unparse(commit_fact.args)
    assert not any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "cast"
        for node in ast.walk(commit_fact)
    )
