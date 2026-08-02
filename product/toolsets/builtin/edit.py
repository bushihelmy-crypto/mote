"""Model-facing adapter for immutable managed file edit plans."""

from __future__ import annotations

from typing import ClassVar

from mote.contracts.file import CreateMutation, FileOperationError, ReplaceMutation, TransactionStatus
from mote.contracts.tool.errors import ToolError
from mote.contracts.tool.result import json_tool_payload
from mote.product.toolsets.builtin._paths import resolve_path, resolve_permission_path
from mote.runtime.fileops.edit_plans import (
    AbsentEditPlanSource,
    EditPlanManifestError,
    EditPlanOutputLimitError,
    EditPlanSourceError,
    LiteralEditPlanRequest,
    ReplacementLimitExceededError,
    WholeFileEditPlanRequest,
)
from mote.runtime.fileops.identity import path_token
from mote.runtime.presentation import count_noun, verb_agree
from mote.runtime.tools.base_tool import BaseTool
from mote.runtime.tools.capability_types import CommitEditPlan, GetCwd, PlanFileEdit
from mote.runtime.tools.tool_result import FileChange, ToolResult

_MSG_FILE_PATH_REQUIRED = "Error: 'file_path' argument is required."
_MSG_STRINGS_REQUIRED = "Error: 'old_string' and 'new_string' are required."
_MSG_STRINGS_MUST_BE_STR = "Error: 'old_string' and 'new_string' must be strings."
_MSG_NO_CHANGES = "Error: no changes to make — old_string and new_string are identical."
_MSG_IS_NOTEBOOK = "Error: '{path}' is a Jupyter notebook. Edit does not support substring " "changes to .ipynb files."
_MSG_CANNOT_WRITE = "Error: cannot write '{path}': {error}"
_MSG_NOT_COMMITTED = "Error: edit transaction {transaction_id} ended as {status}: {detail}"
_MSG_INVALID_OUTCOME = "Error: edit plan returned an invalid single-file outcome."
_MSG_UPDATED_ALL = "The file {path} has been updated. All {count} {verb} successfully replaced."
_MSG_UPDATED = "The file {path} has been updated successfully."
_MSG_WHOLE_FILE_OK = "{verb} {path} ({lines}, {size} bytes written)."


class Edit(BaseTool):
    """Create, overwrite, or exactly edit one file through File Operations."""

    name = "Edit"
    aliases: ClassVar[list[str]] = [
        "Edit.run",
        "edit",
        "Update",
        "update",
        "Write",
        "Write.run",
        "write",
    ]
    reconstructable: ClassVar[bool] = True
    max_result_size_chars: ClassVar[int] = 100_000
    requires = ("plan_file_edit", "commit_edit_plan", "get_cwd")
    risk_level = "high"
    mutates_filesystem = True

    plan_file_edit: PlanFileEdit
    commit_edit_plan: CommitEditPlan
    get_cwd: GetCwd

    def permission_target(self, args: dict) -> str:
        """The canonical target path matched by Edit path rules and sandboxing."""
        return resolve_permission_path(self.get_cwd, args.get("file_path"))

    async def call(
        self,
        *,
        file_path: str,
        new_string: str,
        old_string: str = "",
        replace_all: bool = False,
    ) -> ToolResult:
        """Create, overwrite, or replace exact text in one file.

        An empty ``old_string`` means whole-file create/overwrite. A non-empty
        ``old_string`` is matched exactly and must occur once unless
        ``replace_all`` is true. Existing files must have been read in this
        session; creation requires an existing parent directory.

        Args:
            file_path: Absolute path or path relative to the working directory.
            new_string: Replacement text, or the complete file content when
                old_string is empty.
            old_string: Exact text to replace. Leave empty to create or overwrite
                the whole file.
            replace_all: Replace every occurrence of old_string instead of
                requiring exactly one match.
        """
        self._validate_arguments(file_path, old_string, new_string)
        full_path = resolve_path(self.get_cwd, file_path.strip())
        if old_string and full_path.endswith(".ipynb"):
            raise ToolError(_MSG_IS_NOTEBOOK.format(path=file_path))
        request = (
            WholeFileEditPlanRequest(
                path=path_token(full_path),
                content=new_string,
            )
            if old_string == ""
            else LiteralEditPlanRequest(
                path=path_token(full_path),
                old=old_string,
                new=new_string,
                replace_all=replace_all,
            )
        )
        try:
            plan = await self.plan_file_edit(request)
            outcome = await self.commit_edit_plan(plan.plan_id)
        except (
            EditPlanManifestError,
            EditPlanOutputLimitError,
            EditPlanSourceError,
            FileOperationError,
            ReplacementLimitExceededError,
        ) as exc:
            raise ToolError(_MSG_CANNOT_WRITE.format(path=file_path, error=exc)) from exc
        if outcome.result.status != TransactionStatus.COMMITTED:
            raise ToolError(
                _MSG_NOT_COMMITTED.format(
                    transaction_id=outcome.result.transaction_id,
                    status=outcome.result.status.value,
                    detail=outcome.result.detail,
                )
            )
        if len(outcome.changes) != 1 or len(plan.mutation_set.mutations) != 1:
            raise ToolError(_MSG_INVALID_OUTCOME)
        change = outcome.changes[0]
        mutation = plan.mutation_set.mutations[0]
        if not isinstance(mutation, (CreateMutation, ReplaceMutation)):
            raise ToolError(_MSG_INVALID_OUTCOME)
        if old_string == "":
            created = isinstance(plan.sources[0], AbsentEditPlanSource)
            line_count = change.new.count("\n") + (1 if change.new and not change.new.endswith("\n") else 0)
            message = _MSG_WHOLE_FILE_OK.format(
                verb="Created" if created else "Updated",
                path=change.path.display,
                lines=count_noun(line_count, "line"),
                size=mutation.after.size,
            )
        elif replace_all:
            count = plan.preview.total_replacements
            message = _MSG_UPDATED_ALL.format(
                path=change.path.display,
                count=count_noun(count, "occurrence"),
                verb=verb_agree(count, "was", "were"),
            )
        else:
            message = _MSG_UPDATED.format(path=change.path.display)
        return ToolResult(
            output=message,
            file_changes=[
                FileChange(
                    path=change.path.display,
                    old=change.old,
                    new=change.new,
                    transaction_id=outcome.result.transaction_id,
                    post_digest=change.post_digest,
                )
            ],
            payload=json_tool_payload({"transaction_id": outcome.result.transaction_id}),
        )

    @staticmethod
    def _validate_arguments(file_path, old_string, new_string) -> None:
        if not file_path or not file_path.strip():
            raise ToolError(_MSG_FILE_PATH_REQUIRED)
        if old_string is None or new_string is None:
            raise ToolError(_MSG_STRINGS_REQUIRED)
        if not isinstance(old_string, str) or not isinstance(new_string, str):
            raise ToolError(_MSG_STRINGS_MUST_BE_STR)
        if old_string != "" and old_string == new_string:
            raise ToolError(_MSG_NO_CHANGES)
