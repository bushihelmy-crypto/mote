import ast
from pathlib import Path


def test_interaction_core_has_no_private_capability_probing() -> None:
    root = Path(__file__).parents[2]
    driver = (root / "product/interaction/driver.py").read_text(encoding="utf-8")
    channel = (root / "product/interaction/human_channel.py").read_text(encoding="utf-8")
    assert "hasattr(" not in driver
    assert "setattr(" not in driver
    assert "getattr(" not in driver
    assert "hasattr(" not in channel
    assert "except TypeError" not in channel
    assert "bind_driver_control(" in driver
    assert "from typing import Any" not in channel
    assert "_degrade_ask_questions" not in channel
    assert "def set_addresses" not in channel
    assert "def publish_message" not in channel
    assert "roles:" not in channel


def test_input_port_owns_explicit_lifecycle_capabilities() -> None:
    root = Path(__file__).parents[2]
    source = (root / "product/interaction/ports.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    input_port = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "InputPort")
    methods = {node.name for node in input_port.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert {"start", "aclose", "take_turn_images", "request_exit"} <= methods


def test_turn_runner_has_no_reflective_contract_negotiation() -> None:
    root = Path(__file__).parents[2]
    turn = (root / "product/interaction/turn.py").read_text(encoding="utf-8")
    assert "getattr(" not in turn
    assert "hasattr(" not in turn


def test_connection_scope_uses_typed_edges_and_explicit_close() -> None:
    root = Path(__file__).parents[2]
    source = (root / "product/session_hosting/connection.py").read_text(encoding="utf-8")
    assert "List[Any]" not in source
    assert "port: Any" not in source
    assert "message: Any" not in source
    assert "getattr(" not in source
    assert "PresentationConsumer" in source
    assert "InputPort" in source
