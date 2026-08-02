from pathlib import Path


def test_remote_session_adapters_use_explicit_registry_operations() -> None:
    root = Path(__file__).parents[2]
    registry = (root / "product/session_hosting/registry.py").read_text(encoding="utf-8")
    acp = (root / "product/interfaces/acp/server.py").read_text(encoding="utf-8")
    agui = (root / "product/interfaces/agui/server.py").read_text(encoding="utf-8")
    assert "def get_or_create(" not in registry
    for operation in ("create_new", "get_resident", "load_existing", "fork_existing"):
        assert f"def {operation}(" in registry
    assert "get_or_create(" not in acp
    assert "get_or_create(" not in agui
    assert "_fork_role" not in acp
    assert "except Exception as exc:  # noqa: BLE001 — a turn failure" not in acp


def test_remote_adapters_do_not_reimplement_durable_load_validation() -> None:
    root = Path(__file__).parents[2]
    sources = "\n".join(
        (root / path).read_text(encoding="utf-8")
        for path in ("product/interfaces/acp/server.py", "product/interfaces/agui/server.py")
    )
    assert "ResidencyStore" not in sources
    assert "SessionLog" not in sources
    assert "definition_digest" not in sources
