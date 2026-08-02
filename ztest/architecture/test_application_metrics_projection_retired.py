from pathlib import Path


def test_unwired_application_metrics_projection_is_retired() -> None:
    root = Path(__file__).parents[2]
    assert not (root / "runtime/telemetry/application_metrics.py").exists()
    production = tuple(
        (root / layer).rglob("*.py") for layer in ("contracts", "kernel", "runtime", "orchestration", "product")
    )
    paths = (path for group in production for path in group)
    assert all("ApplicationMetricsProjection" not in path.read_text(encoding="utf-8") for path in paths)
