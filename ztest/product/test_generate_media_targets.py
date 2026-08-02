from __future__ import annotations

import pytest

from mote.contracts.file import MutationResult, TransactionStatus
from mote.contracts.service import MediaGenerationResult
from mote.contracts.tool import ToolAttemptOrdinal, ToolInvocationId, ToolInvocationIdentity
from mote.product.toolsets.builtin.generate_media.generate_media_tool import GenerateMedia, _generation_operation_key
from mote.product.toolsets.builtin.generate_media.target_plan import MediaTargetDisposition, plan_media_targets
from mote.runtime.session.workspace import SessionWorkspace
from mote.runtime.tools.execution_context import AuthorizedToolInvocation, bind_authorized_invocation
from mote.runtime.tools.permission.config import PermissionConfig, SandboxConfig
from mote.runtime.tools.policy import build_tool_call_policy
from mote.runtime.tools.tool_result import ToolResult
from mote.ztest.executor.conftest import make_executor
from mote.ztest.executor.tools.conftest import CapRole
from mote.ztest.fileops_factory import FileOperations


def test_duplicate_and_default_names_are_deterministic_and_keep_extensions(tmp_path):
    plans = plan_media_targets(
        cwd=str(tmp_path),
        output_dir="out",
        items_by_kind=(
            ("image", ({"filename": "asset.tar.png"}, {"filename": "asset.tar.png"})),
            ("audio", ({}, {})),
        ),
    )
    assert [plan.item_id for plan in plans] == ["image:0", "image:1", "audio:0", "audio:1"]
    assert plans[0].resolved_target.endswith("asset.tar.png")
    assert plans[1].resolved_target.endswith("asset-2.tar.png")
    assert plans[2].resolved_target.endswith("audio.mp3")
    assert plans[3].resolved_target.endswith("audio-2.mp3")
    assert plans[0].disposition is MediaTargetDisposition.REQUESTED
    assert plans[1].disposition is MediaTargetDisposition.RENAMED


def test_retry_rebuilds_identical_plan(tmp_path):
    request = dict(
        cwd=str(tmp_path),
        output_dir="exports",
        items_by_kind=(("video", ({"filename": "clip.mp4"}, {"filename": "clip.mp4"})),),
    )
    assert plan_media_targets(**request) == plan_media_targets(**request)
    first = plan_media_targets(**request)[0]
    renamed = plan_media_targets(**request, collision_round=2)[0]
    assert _generation_operation_key("video", 0, first) == _generation_operation_key("video", 0, first)
    assert _generation_operation_key("video", 0, first) != _generation_operation_key("video", 0, renamed)


def test_permission_targets_are_exactly_the_planned_write_targets(tmp_path):
    tool = GenerateMedia(object())
    tool.get_cwd = lambda: str(tmp_path)
    args = {
        "images": [
            {"filename": "same.png", "description": "first"},
            {"filename": "same.png", "description": "second"},
        ],
        "output_dir": "generated",
    }
    targets = tool.permission_targets(args)
    assert tool.mutates_filesystem_for(args) is True
    assert targets[0].endswith("same.png")
    assert targets[1].endswith("same-2.png")
    assert tool.permission_targets(args) == targets


def test_remote_only_call_declares_no_file_mutation():
    tool = GenerateMedia(object())
    assert tool.mutates_filesystem_for({"images": [{}]}) is False
    assert tool.permission_targets({"images": [{}]}) == []


def test_fileops_target_reservation_is_atomic_across_instances(tmp_path):
    lock_root = tmp_path / "locks"
    first = FileOperations(
        session_id="first",
        journal_path=tmp_path / "first" / "rollout.jsonl",
        get_project_root=lambda: str(tmp_path),
        lock_root=lock_root,
    )
    second = FileOperations(
        session_id="second",
        journal_path=tmp_path / "second" / "rollout.jsonl",
        get_project_root=lambda: str(tmp_path),
        lock_root=lock_root,
    )
    target = str(tmp_path / "asset.png")
    held = first.try_reserve_generated_targets((target,))
    assert held is not None
    assert second.try_reserve_generated_targets((target,)) is None
    held.release()
    acquired = second.try_reserve_generated_targets((target,))
    assert acquired is not None
    acquired.release()


def test_fileops_reservation_rejects_outside_and_symlink_escape(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / "escape").symlink_to(outside, target_is_directory=True)
    operations = FileOperations(
        session_id="session",
        journal_path=tmp_path / "session" / "rollout.jsonl",
        get_project_root=lambda: str(project),
        lock_root=tmp_path / "locks",
    )
    with pytest.raises(ValueError, match="outside project root"):
        operations.try_reserve_generated_targets((str(outside / "asset.png"),))
    with pytest.raises(ValueError, match="outside project root"):
        operations.try_reserve_generated_targets((str(project / "escape" / "asset.png"),))


def test_concurrent_permission_plans_resolve_to_disjoint_targets(tmp_path):
    (tmp_path / "out").mkdir()
    lock_root = tmp_path / "locks"
    operations = [
        FileOperations(
            session_id=name,
            journal_path=tmp_path / name / "rollout.jsonl",
            get_project_root=lambda: str(tmp_path),
            lock_root=lock_root,
        )
        for name in ("first", "second")
    ]
    tools = [GenerateMedia(object()), GenerateMedia(object())]
    for tool, fileops in zip(tools, operations, strict=True):
        tool.get_cwd = lambda: str(tmp_path)
        tool.try_reserve_generated_targets = fileops.try_reserve_generated_targets
    arguments = {
        "images": [
            {"filename": "same.png", "description": "first"},
            {"filename": "same.png", "description": "second"},
        ],
        "output_dir": "out",
    }
    identities = [
        ToolInvocationIdentity(
            ToolInvocationId(name),
            ToolAttemptOrdinal(1),
            "definition",
            1,
            "digest",
            name,
            "run",
        )
        for name in ("first", "second")
    ]
    first = tools[0].permission_facts(arguments, identities[0])
    second = tools[1].permission_facts(arguments, identities[1])
    assert set(first.targets).isdisjoint(second.targets)
    assert [target.rsplit("/", 1)[-1] for target in first.targets] == ["same.png", "same-2.png"]
    assert [target.rsplit("/", 1)[-1] for target in second.targets] == ["same-3.png", "same-4.png"]
    tools[0].release_permission_facts(identities[0])
    tools[1].release_permission_facts(identities[1])


class _Configured:
    base_url = "https://media.invalid"
    api_key = "configured"  # pragma: allowlist secret
    model = "model"


class _Multimodal:
    image_generation = _Configured()


@pytest.mark.asyncio
async def test_each_asset_has_independent_typed_publication_settlement(monkeypatch, tmp_path):
    tool = GenerateMedia(_Multimodal())
    tool.get_cwd = lambda: str(tmp_path)
    requested: list[str] = []

    async def invoke_service(*args, **kwargs):
        return MediaGenerationResult(filename="same.png", url="https://asset.invalid/x")

    transactions: list[str] = []

    async def commit(files, *, source, transaction_id=None):
        target = next(iter(files))
        requested.append(target)
        transactions.append(transaction_id)
        status = TransactionStatus.COMMITTED if len(requested) == 1 else TransactionStatus.ABORTED
        return MutationResult(source, status, detail="collision" if status is TransactionStatus.ABORTED else "")

    async def download(_result):
        return b"media"

    tool.invoke_service = invoke_service
    tool.commit_generated_files = commit
    released: list[bool] = []

    class Reservation:
        def release(self):
            released.append(True)

    tool.try_reserve_generated_targets = lambda targets: Reservation()
    monkeypatch.setattr(
        "mote.product.toolsets.builtin.generate_media.generate_media_tool._download",
        download,
    )
    invocation = AuthorizedToolInvocation(
        identity=ToolInvocationIdentity(
            ToolInvocationId("call"),
            ToolAttemptOrdinal(1),
            "definition",
            1,
            "digest",
            "owner",
            "run",
        ),
        tool_name="GenerateMedia",
        arguments={},
        generation=1,
    )
    arguments = {
        "images": [
            {"filename": "same.png", "description": "first"},
            {"filename": "same.png", "description": "second"},
        ],
        "output_dir": "out",
    }
    tool.permission_facts(arguments, invocation.identity)
    with bind_authorized_invocation(invocation):
        result = await tool.call(
            images=arguments["images"],
            output_dir="out",
        )
    tool.release_permission_facts(invocation.identity)
    assert isinstance(result, ToolResult)
    payload = result.payload.materialize()
    assets = payload["images"]["assets"]
    assert [item["publication"]["publication_disposition"] for item in assets] == ["committed"]
    assert assets[0]["local_path"].endswith("same.png")
    assert payload["images"]["failed"] == [{"filename": "same.png", "error": "collision"}]
    assert requested[1].endswith("same-2.png")
    assert len(set(transactions)) == 2
    assert all(value.startswith("generate-media-") for value in transactions)
    assert result.output == "Media generation completed."
    assert released == [True]


@pytest.mark.asyncio
async def test_read_only_denies_before_remote_and_releases_reservation(tmp_path):
    (tmp_path / "out").mkdir()
    role = CapRole(cwd=str(tmp_path))
    tool = GenerateMedia(_Multimodal())
    remote_calls = 0

    async def invoke_service(**kwargs):
        nonlocal remote_calls
        remote_calls += 1
        return {"status": "success", "url": "https://asset.invalid/x"}

    base_capabilities = role.tool_capabilities
    role.tool_capabilities = lambda: {**base_capabilities(), "invoke_service": invoke_service}
    policy = build_tool_call_policy(
        PermissionConfig(
            mode="dontAsk",
            sandbox=SandboxConfig(mode="read-only"),
        ),
        role=role,
        require_permission=True,
    )
    executor = make_executor(
        tool,
        session_id="media",
        role=role,
        tool_call_policy=policy,
        workspace_store=SessionWorkspace(root=tmp_path / "sessions"),
    )
    result = await executor.run_command(
        "GenerateMedia",
        {"images": [{"filename": "asset.png"}], "output_dir": "out"},
        result_id="media-call",
    )
    assert result.success is False
    assert remote_calls == 0
    reservation = role.file_operations.try_reserve_generated_targets((str(tmp_path / "out" / "asset.png"),))
    assert reservation is not None
    reservation.release()


@pytest.mark.asyncio
async def test_in_doubt_publication_is_not_reported_as_committed(monkeypatch, tmp_path):
    tool = GenerateMedia(_Multimodal())
    tool.get_cwd = lambda: str(tmp_path)

    async def invoke_service(*args, **kwargs):
        return MediaGenerationResult(filename="asset.png", url="https://asset.invalid/x")

    async def commit(files, *, source, transaction_id=None):
        return MutationResult(transaction_id, TransactionStatus.IN_DOUBT, detail="commit receipt lost")

    class Reservation:
        targets: tuple[str, ...] = ()

        def release(self):
            return None

    async def download(_result):
        return b"media"

    tool.invoke_service = invoke_service
    tool.commit_generated_files = commit
    tool.try_reserve_generated_targets = lambda targets: Reservation()
    monkeypatch.setattr(
        "mote.product.toolsets.builtin.generate_media.generate_media_tool._download",
        download,
    )
    identity = ToolInvocationIdentity(
        ToolInvocationId("in-doubt"),
        ToolAttemptOrdinal(1),
        "definition",
        1,
        "digest",
        "owner",
        "run",
    )
    arguments = {
        "images": [{"filename": "asset.png", "description": "asset"}],
        "output_dir": str(tmp_path),
    }
    tool.permission_facts(arguments, identity)
    invocation = AuthorizedToolInvocation(identity, "GenerateMedia", arguments, 1)
    with bind_authorized_invocation(invocation), pytest.raises(RuntimeError, match="All media generation failed"):
        await tool.call(images=arguments["images"], output_dir=str(tmp_path))
