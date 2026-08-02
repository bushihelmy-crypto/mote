from pathlib import Path


def test_runtime_composition_has_explicit_typed_generation_lifecycle() -> None:
    root = Path(__file__).parents[2]
    composition = (root / "runtime/models/composition.py").read_text(encoding="utf-8")
    state = (root / "runtime/models/failover/runtime_state.py").read_text(encoding="utf-8")
    lifecycle = (root / "runtime/models/generation_lifecycle.py").read_text(encoding="utf-8")

    assert "getattr(" not in composition
    assert "inspect.isawaitable" not in composition
    assert "tuple[object" not in state
    assert "getattr(" not in lifecycle
    assert "inspect.isawaitable" not in lifecycle
    assert "AsyncGenerationResource" in state
    assert "Generic[ReuseKeyT]" in composition


def test_runtime_composition_factory_types_decorator_and_reuse_identity() -> None:
    root = Path(__file__).parents[2]
    composition = (root / "runtime/models/composition.py").read_text(encoding="utf-8")

    assert "gateway_decorator: Callable[[ModelGateway], ModelGateway] | None" in composition
    assert "reuse_key: ReuseKeyT" in composition
    assert "def reuse_key(self) -> ReuseKeyT" in composition
