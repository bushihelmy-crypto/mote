"""Persistent vector Canvas builtin backed by the shared RuntimeHost."""

from __future__ import annotations

import hashlib
import json
import os
from typing import ClassVar, Literal
from uuid import uuid4

from mote.contracts.artifact import (
    ArtifactPublishRequest,
    ArtifactRepresentationInput,
    ArtifactRetention,
    ArtifactSensitivity,
)
from mote.contracts.authorization import PermissionDecision
from mote.contracts.file import TransactionStatus
from mote.contracts.runtime import RuntimeAccessMode, RuntimeProjectionIntent
from mote.contracts.runtime.errors import ManagedRuntimeNotFoundError
from mote.contracts.surface import CanvasDocument, CanvasOperation
from mote.contracts.tool.effects import ToolEffect
from mote.contracts.tool.errors import ToolError
from mote.contracts.tool.result import json_tool_payload
from mote.product.toolsets.builtin._paths import resolve_path
from mote.product.toolsets.builtin.runtime_action import is_handoff_action, run_handoff_action
from mote.runtime.interactive.canvas.driver import CanvasRuntimeDriver
from mote.runtime.projections import artifact_representation_set_digest
from mote.runtime.tools.base_tool import BaseTool
from mote.runtime.tools.capability_types import (
    CommitGeneratedFiles,
    GetArtifactPublisher,
    GetCwd,
    GetRuntimeHost,
    HandoffRuntime,
)
from mote.runtime.tools.execution_context import current_tool_call_id
from mote.runtime.tools.tool_result import ToolMedia, ToolResult

_RUNTIME = "canvas:default"
_MSG_INVALID_OPERATIONS = "Error: invalid canvas operations — {error}"
_MSG_UNKNOWN_ACTION = "Error: unknown canvas action '{action}'. Use handoff or leave action empty."
_MSG_EXPORT_DIR_REQUIRED = "Error: export_formats requires output_dir."
_MSG_EXPORT_FAILED = (
    "Canvas revision {revision} was committed, but its exports could not be "
    "completed during {stage}: {error}. Do not replay the operations; observe the "
    "current canvas with an empty operations list to retry the export."
)


class Canvas(BaseTool):
    """Create and edit one persistent interactive vector canvas."""

    name = "Canvas"
    aliases: list[str] = []
    keywords: ClassVar[list[str]] = [
        "draw",
        "diagram",
        "flowchart",
        "visualize",
        "whiteboard",
        "画图",
        "图表",
        "流程图",
        "画板",
    ]
    requires = (
        "get_runtime_host",
        "get_artifact_publisher",
        "get_cwd",
        "commit_generated_files",
        "handoff_runtime",
    )
    stateful = True
    effect = ToolEffect.LOCAL

    get_runtime_host: GetRuntimeHost
    get_artifact_publisher: GetArtifactPublisher
    get_cwd: GetCwd
    commit_generated_files: CommitGeneratedFiles
    handoff_runtime: HandoffRuntime

    async def call(
        self,
        *,
        action: str = "",
        operations: list[CanvasOperation] | None = None,
        width: int = 1200,
        height: int = 800,
        export_formats: list[Literal["png", "drawio"]] | None = None,
        output_dir: str | None = None,
        close: bool = False,
        message: str = "",
    ) -> ToolResult:
        """Apply one atomic canvas update and optionally export it locally.

        Operations run in order and commit as one revision. Reuse stable element
        ids to update shapes. An empty batch observes without changing the scene.
        Use action=handoff to give the user exclusive control of an already-open
        canvas and wait until control returns.

        Shape geometry:
        - rect/ellipse use x, y, width and height.
        - line/arrow use x, y as the start and x2, y2 as the end.
          source_id/target_id optionally keep endpoints attached to shapes.
        - text uses x, y and text; font size lives in style.font_size.

        Args:
            action: Set to handoff for direct user interaction with the live
                canvas; otherwise leave empty.
            operations: Ordered upsert, remove, or clear operations.
            width: Initial width; used only when creating the canvas.
            height: Initial height; used only when creating the canvas.
            export_formats: Additional local formats; requires output_dir. SVG is included.
            output_dir: Existing export directory; omit unless the user requests files.
            close: Close and discard the canvas; ignores operations.
            message: Optional instructions shown to the user during handoff.
        """
        action = (action or "").strip().lower()
        if action == "handoff":
            handoff_result = await run_handoff_action(self.handoff_runtime, _RUNTIME, message=message)
            if not handoff_result.success:
                return handoff_result
            host = self.get_runtime_host()
            descriptor = host.descriptor(_RUNTIME)
            async with host.access(
                descriptor.ref,
                mode=RuntimeAccessMode.READ,
                owner_id=f"agent:{self.session_id}:canvas-observe",
                expected_revision=descriptor.revision,
            ) as access:
                driver = access.driver
                if not isinstance(driver, CanvasRuntimeDriver):
                    raise RuntimeError("canvas runtime has an unexpected driver")
                document = driver.snapshot_document()
            return ToolResult(
                output=(
                    f"{handoff_result.output}\n"
                    f"Current canvas after handoff (revision {descriptor.revision}):\n"
                    f"{document.model_dump_json()}"
                ),
                success=handoff_result.success,
                payload=json_tool_payload(document.model_dump(mode="json")),
            )
        if action:
            raise ToolError(_MSG_UNKNOWN_ACTION.format(action=action))
        host = self.get_runtime_host()
        if close:
            try:
                host.descriptor(_RUNTIME)
            except ManagedRuntimeNotFoundError:
                return ToolResult(output="[no canvas to close]")
            await host.close(_RUNTIME)
            return ToolResult(output="[canvas closed]")

        if export_formats and (not isinstance(output_dir, str) or not output_dir.strip()):
            raise ToolError(_MSG_EXPORT_DIR_REQUIRED)

        try:
            typed_operations = [
                (operation if isinstance(operation, CanvasOperation) else CanvasOperation.model_validate(operation))
                for operation in operations or []
            ]
        except Exception as exc:  # noqa: BLE001 — convert boundary validation to a tool error
            raise ToolError(_MSG_INVALID_OPERATIONS.format(error=exc))

        try:
            descriptor = host.descriptor(_RUNTIME)
        except ManagedRuntimeNotFoundError:
            descriptor = await host.ensure(CanvasRuntimeDriver(CanvasDocument(width=width, height=height)))

        affected: tuple[str, ...] = ()
        formats = ("svg", *dict.fromkeys(export_formats or ()))
        changed = False
        commit_id: str | None = None
        mode = RuntimeAccessMode.WRITE if typed_operations else RuntimeAccessMode.READ
        async with host.access(
            descriptor.ref,
            mode=mode,
            owner_id=f"agent:{self.session_id}:canvas",
            expected_revision=descriptor.revision,
        ) as access:
            driver = access.driver
            if not isinstance(driver, CanvasRuntimeDriver):
                raise RuntimeError("canvas runtime has an unexpected driver")
            if typed_operations:
                tool_call_id = current_tool_call_id()
                operation_id = (
                    "canvas-" + hashlib.sha256(f"{self.session_id}\0{tool_call_id}".encode("utf-8")).hexdigest()
                    if tool_call_id
                    else f"canvas-direct-{uuid4().hex}"
                )
                should_apply = await access.prepare_operation(
                    operation_id=operation_id,
                    codec="canvas-operations+json@1",
                    schema_version=1,
                    payload=json.dumps(
                        [item.model_dump(mode="json") for item in typed_operations],
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    projections=(
                        RuntimeProjectionIntent(
                            intent_id="artifact",
                            projector="canvas-artifact",
                            schema_version=1,
                            options=(("formats", ",".join(formats)),),
                        ),
                    ),
                )
                if should_apply:
                    changed, affected = driver.apply(typed_operations)
                    access.commit(changed=changed)
            document = driver.snapshot_document()
        commit_id = access.result_commit_id

        revision = host.descriptor(descriptor.ref).revision
        failed_stage = "canvas_export"
        try:
            exports = await driver.export_representations(document, formats)
            preview_export = next(
                (export for export in exports if export.representation == "png"),
                next(export for export in exports if export.representation == "svg"),
            )
            representations = tuple(
                ArtifactRepresentationInput(
                    representation=export.representation,
                    kind="canvas",
                    mime_type=export.mime_type,
                    content=export.content,
                    suggested_name=export.suggested_name,
                )
                for export in exports
            )
            state_digest = artifact_representation_set_digest(representations)
            artifact_id = f"canvas-{state_digest}"
            request = ArtifactPublishRequest(
                artifact_id=artifact_id,
                expected_revision=0,
                retention=ArtifactRetention.SESSION,
                sensitivity=ArtifactSensitivity.PRIVATE,
                representations=representations,
            )
            failed_stage = "artifact_publish"
            artifact_revision = await self.get_artifact_publisher().publish(
                artifact_id,
                request,
            )
            if commit_id is not None:
                await host.acknowledge_projection(commit_id, "artifact")
            preview_artifact = artifact_revision.get(preview_export.representation)
            local_paths: dict[str, str] = {}
            if output_dir:
                failed_stage = "local_export"
                destination = resolve_path(self.get_cwd, output_dir.strip())
                local_paths = {
                    export.representation: os.path.join(destination, f"canvas.{export.representation}")
                    for export in exports
                }
                materialized = await self.commit_generated_files(
                    {local_paths[export.representation]: export.content for export in exports},
                    source="Canvas",
                )
                if materialized.status is not TransactionStatus.COMMITTED:
                    raise RuntimeError(
                        f"file transaction {materialized.transaction_id} ended as "
                        f"{materialized.status.value}: {materialized.detail}"
                    )
        except Exception as exc:  # noqa: BLE001 — mark the committed partial success
            raise ToolError(
                _MSG_EXPORT_FAILED.format(
                    revision=revision,
                    stage=failed_stage,
                    error=exc,
                ),
                cause=exc,
                partial_success=True,
                committed_runtime=_RUNTIME,
                committed_revision=revision,
                failed_stage=failed_stage,
            ) from exc
        action = "updated" if changed else "observed"
        output = f"Canvas {action}: {len(document.elements)} elements, revision {revision}, " f"runtime {_RUNTIME}."
        if affected:
            output += " Affected: " + ", ".join(affected) + "."
        if local_paths:
            output += " Exported: " + ", ".join(local_paths.values()) + "."
        return ToolResult(
            output=output,
            payload=json_tool_payload(document.model_dump(mode="json")),
            media=(
                [
                    ToolMedia(
                        kind="image",
                        mime=preview_export.mime_type,
                        artifact=preview_artifact,
                        ref=local_paths[preview_export.representation],
                    )
                ]
                if local_paths
                else []
            ),
        )

    def mutates_filesystem_for(self, args: dict) -> bool:
        return not is_handoff_action(args) and bool(args.get("output_dir")) and not args.get("close")

    def permission_targets(self, args: dict) -> list[str]:
        if is_handoff_action(args) or args.get("close"):
            return []
        output_dir = args.get("output_dir")
        if not isinstance(output_dir, str) or not output_dir.strip():
            return []
        destination = resolve_path(self.get_cwd, output_dir.strip())
        formats = ("svg", *dict.fromkeys(args.get("export_formats") or ()))
        return [os.path.realpath(os.path.join(destination, f"canvas.{format}")) for format in formats]

    def check_permissions(self, args: dict) -> PermissionDecision | None:
        return PermissionDecision.allow("tool_check", "Canvas is pre-approved")

    def resolve_effect_for(self, args: dict) -> ToolEffect:
        if is_handoff_action(args):
            return ToolEffect.EXTERNAL
        return self.resolve_effect()

    async def cleanup_session(self, session_id: str) -> None:
        host = self.get_runtime_host()
        try:
            host.descriptor(_RUNTIME)
        except ManagedRuntimeNotFoundError:
            return
        await host.close(_RUNTIME)


__all__ = ["Canvas"]
