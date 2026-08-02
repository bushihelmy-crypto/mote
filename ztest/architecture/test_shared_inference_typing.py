from pathlib import Path

from mote.product.inference.daemon.messages import FiniteExecution, SessionExecution

ROOT = Path(__file__).resolve().parents[2]


def test_protobuf_and_dynamic_dispatch_stop_at_grpc_adapter() -> None:
    internal = (
        "client_port.py",
        "execution_backend.py",
        "generation.py",
        "messages.py",
        "reconnecting_client.py",
        "shared_runtime.py",
    )
    for name in internal:
        source = (ROOT / "product/inference/daemon" / name).read_text(encoding="utf-8")
        assert "gateway_v1_pb2" not in source
        assert "hasattr(" not in source
        assert "import Any" not in source
        assert ": Any" not in source


def test_execution_variants_expose_disjoint_control_surfaces() -> None:
    finite = set(FiniteExecution.__dict__)
    session = set(SessionExecution.__dict__)

    assert {"authorize_wire", "cancel"} <= finite
    assert not {"authorize_open", "send", "close"} & finite
    assert {"authorize_open", "send", "close"} <= session
    assert not {"authorize_wire", "cancel"} & session
