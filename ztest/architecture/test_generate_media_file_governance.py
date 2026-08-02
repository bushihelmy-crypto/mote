from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_generate_media_uses_fileops_capability_without_direct_writes():
    source = (ROOT / "product/toolsets/builtin/generate_media/generate_media_tool.py").read_text(encoding="utf-8")
    assert '"commit_generated_files"' in source
    assert '"try_reserve_generated_targets"' in source
    assert "def mutates_filesystem_for" in source
    assert "def permission_targets" in source
    assert "await self.commit_generated_files(" in source
    assert "_generation_operation_key(kind, index, plan)" in source
    assert "+ plan.requested_target" in source
    assert "+ plan.resolved_target" in source
    hosted_payload = (ROOT / "contracts/service/operations.py").read_text(encoding="utf-8")
    media_payload = hosted_payload[
        hosted_payload.index("class MediaGenerationPayload") : hosted_payload.index("class WebSearchPayload")
    ]
    assert "target_plan" not in media_payload
    assert ".open(" not in source
    assert ".write_bytes(" not in source
    assert ".write_text(" not in source
    assert "mkdir(" not in source
    assert 'materialization_error"] = str(exc)' not in source


def test_reservation_is_part_of_authorization_and_has_bounded_lifecycle():
    pipeline = (ROOT / "runtime/tools/tool_pipeline.py").read_text(encoding="utf-8")
    fileops = (ROOT / "runtime/fileops/facade.py").read_text(encoding="utf-8")
    assert "ToolPermissionFactsProvider" in pipeline
    assert "tool.permission_facts(args, execution.identity)" in pipeline
    assert "provider.release_permission_facts(execution.identity)" in pipeline
    assert "def try_reserve_generated_targets(" in fileops
    assert "timeout=0.0, reentrant=False" in fileops
