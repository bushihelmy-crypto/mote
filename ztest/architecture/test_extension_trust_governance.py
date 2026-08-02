"""Static gates for the single Product extension trust boundary."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_all_extension_consumers_use_canonical_source_policy() -> None:
    consumers = (
        "product/agents/markdown_loader.py",
        "product/skills/skill_pool.py",
        "product/composition/container.py",
        "product/composition/agent_factory.py",
    )
    for relative in consumers:
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "admitted_files(" in source, relative


def test_hook_and_mcp_adapters_reject_raw_paths() -> None:
    for relative in ("product/config/adapters/hooks.py", "product/config/adapters/mcp.py"):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "Sequence[ExtensionSource]" in source
        assert "decode_json_section(" in source
