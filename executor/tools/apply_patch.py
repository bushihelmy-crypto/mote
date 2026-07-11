"""ApplyPatch — structured multi-file code edits (a port of codex ``apply_patch``).

A functional superset of the single-shot ``Edit`` / ``Write`` tools: one call can
Add, Update, Delete, and Move/rename multiple files. Edits are located by
*content context anchors* (not line numbers, not a uniqueness burden) and applied
with a 5-pass fuzzy matcher that tolerates whitespace and typographic drift.

The model emits one freeform ``input`` string in codex's patch format; our ported
parser does the structured interpretation, so the same tool works on both the
native tool-use channel (the patch is a JSON string value) and the XML command
channel (the patch rides as the single raw-text arg — no JSON escaping).

Safety integration (this fork):
- Each touched path is evaluated against the permission engine per-path and
  folded strictest-wins into one consolidated approval (see
  ``PermissionEngine.check_multi``).
- The read-before-write mtime guard applies to every Update/Delete/Move source:
  an existing file must have been Read this session and be unchanged on disk.
- The whole patch is transactional: parse + all context anchors are validated and
  every new file content is computed *before any write*. A single unlocatable
  chunk aborts with no partial write.

``Edit`` and ``Write`` are intentionally left untouched — this is additive.
"""
from __future__ import annotations

import os
from typing import ClassVar, List, Tuple

from mote.common.const.tools import MAX_CONTENT_SIZE_BYTES
from mote.common.prompt.tools import APPLY_PATCH_GRAMMAR
from mote.executor.dependency._apply_patch import (
    AddFile,
    ApplyPatchError,
    DeleteFile,
    UpdateFile,
    affected_paths,
    apply_update,
    parse_patch,
)
from mote.executor.dependency._file_base import FileMutatingTool
from mote.executor.tool_registry import register_tool
from mote.executor.tool_result import FileChange, ToolError, ToolResult

# Complete model-facing message sentences, hoisted to module-top templates so the
# wording lives in one place (fill via ``.format(...)`` at the raise site).
# The ``_summary`` assembly fragments (``A/M/D`` rows, counts) stay inline.
_MSG_INPUT_REQUIRED = "Error: 'input' (the patch text) is required."
_MSG_PARSE_FAILED = "Error: failed to parse patch: {error}"
_MSG_NO_OPERATIONS = "Error: the patch contained no file operations."
_MSG_CANNOT_DELETE = "Error: cannot delete '{path}': {error}"
_MSG_CANNOT_REMOVE_ORIGINAL = "Error: cannot remove original '{path}': {error}"
_MSG_CANNOT_ADD = (
    "Error: cannot add '{path}': a file already exists there with content. Use " "an Update File hunk to edit it."
)
_MSG_DELETE_NOT_EXIST = "Error: cannot delete '{path}': file does not exist."
_MSG_UPDATE_NOT_EXIST = "Error: cannot update '{path}': file does not exist."
_MSG_MOVE_DEST_EXISTS = "Error: cannot move '{path}' to '{dest}': the destination already exists."
_MSG_UPDATE_NOT_UTF8 = "Error: cannot update '{path}': file is not valid UTF-8 text."
_MSG_CANNOT_READ = "Error: cannot read '{path}': {error}"
_MSG_CANNOT_APPLY = "Error: cannot apply update to '{path}': {error}"
_MSG_IS_DIRECTORY = "Error: '{path}' is a directory, not a file."
_MSG_IS_NOTEBOOK = "Error: '{path}' is a Jupyter notebook. Use a notebook edit tool to modify " ".ipynb files."
_MSG_RESULT_TOO_LARGE = (
    "Error: resulting content for '{path}' ({size} bytes) exceeds the maximum " "allowed size ({max_size} bytes)."
)
_MSG_CANNOT_MKDIR = "Error: cannot create parent directory for '{path}': {error}"
_MSG_CANNOT_WRITE = "Error: cannot write '{path}': {error}"


@register_tool
class ApplyPatch(FileMutatingTool):
    """Apply a multi-file Add/Update/Delete/Move patch in codex's patch format."""

    name = "ApplyPatch"
    aliases: ClassVar[list[str]] = ["apply_patch", "ApplyPatch.run", "applyPatch"]
    # Summaries can echo many paths; allow a higher cap like Edit/Write.
    max_result_size_chars: ClassVar[int] = 100_000
    description = APPLY_PATCH_GRAMMAR

    # ------------------------------------------------------------------
    # Permission targets (per-path)
    # ------------------------------------------------------------------

    def _affected_abs(self, args: dict) -> List[str]:
        """All absolute paths a patch touches; ``[]`` if it can't be parsed."""
        patch = args.get("input")
        if not isinstance(patch, str) or not patch.strip():
            return []
        try:
            hunks = parse_patch(patch)
        except ApplyPatchError:
            return []
        return [self._resolve(path) for path, _ in affected_paths(hunks)]

    def permission_target(self, args: dict) -> str:
        targets = self._affected_abs(args)
        return targets[0] if targets else ""

    def permission_targets(self, args: dict) -> List[str]:
        return self._affected_abs(args)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def call(self, *, input: str) -> ToolResult:
        """Apply a structured multi-file patch.

        Args:
            input: The full patch text, beginning with ``*** Begin Patch`` and
                ending with ``*** End Patch`` (see the tool description for the
                grammar). Add / Update / Delete / Move operations are applied
                transactionally — if any context anchor cannot be located, the
                whole patch is rejected with no files written.
        """
        if not isinstance(input, str) or not input.strip():
            raise ToolError(_MSG_INPUT_REQUIRED)

        try:
            hunks = parse_patch(input)
        except ApplyPatchError as e:
            raise ToolError(_MSG_PARSE_FAILED.format(error=e))

        if not hunks:
            raise ToolError(_MSG_NO_OPERATIONS)

        # --- Phase 1: resolve + validate + compute (no writes) ---
        plans: List[dict] = []
        for hunk in hunks:
            if isinstance(hunk, AddFile):
                plans.append(self._plan_add(hunk))
            elif isinstance(hunk, DeleteFile):
                plans.append(self._plan_delete(hunk))
            elif isinstance(hunk, UpdateFile):
                plans.append(self._plan_update(hunk))

        # --- Phase 2: apply (writes happen only after every plan validated) ---
        added: List[str] = []
        updated: List[Tuple[str, str]] = []  # (display, "" or "-> dest")
        deleted: List[str] = []
        # Structured facts for the view layer — one per touched file, in patch order.
        # ``old``/``new`` are the display-agnostic content (a creation has old="",
        # a deletion new=""); ``path`` is where the content lives after the op
        # (dest for a move), so a side-by-side host renders the right destination.
        changes: List[FileChange] = []
        for plan in plans:
            op = plan["op"]
            if op == "add":
                self._do_write(plan["full"], plan["content"], "\n")
                self._refresh_read_state(plan["full"])
                added.append(plan["display"])
                changes.append(FileChange(path=plan["full"], old="", new=plan["content"]))
            elif op == "delete":
                self._snapshot_pre_write(plan["full"])
                try:
                    os.unlink(plan["full"])
                except OSError as e:
                    raise ToolError(_MSG_CANNOT_DELETE.format(path=plan["display"], error=e))
                deleted.append(plan["display"])
                changes.append(FileChange(path=plan["full"], old=plan["old"], new=""))
            elif op == "update":
                self._snapshot_pre_write(plan["src"])
                if plan["moved"]:
                    self._do_write(plan["dest"], plan["content"], plan["line_ending"])
                    try:
                        os.unlink(plan["src"])
                    except OSError as e:
                        raise ToolError(_MSG_CANNOT_REMOVE_ORIGINAL.format(path=plan["display"], error=e))
                    self._refresh_read_state(plan["dest"])
                    updated.append((plan["display"], f" -> {plan['move_display']}"))
                else:
                    self._do_write(plan["dest"], plan["content"], plan["line_ending"])
                    self._refresh_read_state(plan["dest"])
                    updated.append((plan["display"], ""))
                changes.append(FileChange(path=plan["dest"], old=plan["old"], new=plan["content"]))

        return ToolResult(output=self._summary(added, updated, deleted), file_changes=changes)

    # ------------------------------------------------------------------
    # Planning (validation) helpers — raise ToolError, never write
    # ------------------------------------------------------------------

    def _plan_add(self, hunk: AddFile) -> dict:
        full = self._resolve(hunk.path)
        self._reject_special(hunk.path, full)
        if os.path.exists(full):
            try:
                with open(full, "r", encoding="utf-8", newline="") as f:
                    existing = f.read()
            except (OSError, UnicodeDecodeError):
                existing = "non-empty"  # unreadable → treat as non-empty, refuse
            if existing.strip() != "":
                raise ToolError(_MSG_CANNOT_ADD.format(path=hunk.path))
        self._guard_size(hunk.path, hunk.contents)
        return {"op": "add", "display": hunk.path, "full": full, "content": hunk.contents}

    def _plan_delete(self, hunk: DeleteFile) -> dict:
        full = self._resolve(hunk.path)
        self._reject_special(hunk.path, full)
        if not os.path.exists(full):
            raise ToolError(_MSG_DELETE_NOT_EXIST.format(path=hunk.path))
        self._check_read_before_write(hunk.path, full, verb="deleting")
        # Read the pre-delete content so the change is a structured fact (old→"").
        try:
            with open(full, "r", encoding="utf-8", newline="") as f:
                old = f.read().replace("\r\n", "\n")
        except (OSError, UnicodeDecodeError):
            old = ""  # unreadable — record an empty before-image rather than fail
        return {"op": "delete", "display": hunk.path, "full": full, "old": old}

    def _plan_update(self, hunk: UpdateFile) -> dict:
        src = self._resolve(hunk.path)
        self._reject_special(hunk.path, src)
        if not os.path.exists(src):
            raise ToolError(_MSG_UPDATE_NOT_EXIST.format(path=hunk.path))
        self._check_read_before_write(hunk.path, src, verb="editing")

        dest = src
        moved = False
        move_display = ""
        if hunk.move_path is not None:
            dest = self._resolve(hunk.move_path)
            self._reject_special(hunk.move_path, dest)
            if os.path.exists(dest):
                raise ToolError(_MSG_MOVE_DEST_EXISTS.format(path=hunk.path, dest=hunk.move_path))
            moved = True
            move_display = hunk.move_path

        line_ending = self._detect_line_ending(src)
        try:
            with open(src, "r", encoding="utf-8", newline="") as f:
                raw = f.read()
        except UnicodeDecodeError:
            raise ToolError(_MSG_UPDATE_NOT_UTF8.format(path=hunk.path))
        except OSError as e:
            raise ToolError(_MSG_CANNOT_READ.format(path=hunk.path, error=e))

        content = raw.replace("\r\n", "\n")
        try:
            new_content = apply_update(content, hunk.chunks)
        except ApplyPatchError as e:
            raise ToolError(_MSG_CANNOT_APPLY.format(path=hunk.path, error=e))

        self._guard_size(hunk.path, new_content)
        return {
            "op": "update",
            "display": hunk.path,
            "src": src,
            "dest": dest,
            "moved": moved,
            "move_display": move_display,
            "old": content,
            "content": new_content,
            "line_ending": line_ending,
        }

    # ------------------------------------------------------------------
    # Shared low-level helpers
    # ------------------------------------------------------------------

    def _resolve(self, path: str) -> str:
        """Resolve a patch path against the stable working directory."""
        return self._resolve_path(path.strip())

    @staticmethod
    def _reject_special(display_path: str, full_path: str) -> None:
        if os.path.isdir(full_path):
            raise ToolError(_MSG_IS_DIRECTORY.format(path=display_path))
        if full_path.endswith(".ipynb"):
            raise ToolError(_MSG_IS_NOTEBOOK.format(path=display_path))

    @staticmethod
    def _guard_size(display_path: str, content: str) -> None:
        size = len(content.encode("utf-8"))
        if size > MAX_CONTENT_SIZE_BYTES:
            raise ToolError(_MSG_RESULT_TOO_LARGE.format(path=display_path, size=size, max_size=MAX_CONTENT_SIZE_BYTES))

    def _do_write(self, full_path: str, content: str, line_ending: str) -> None:
        """Snapshot then write ``content`` to ``full_path``, honoring line ending.

        Creates missing parent directories. ``content`` is LF-normalised; it is
        translated to ``line_ending`` on write.
        """
        self._snapshot_pre_write(full_path)
        parent = os.path.dirname(full_path)
        if parent and not os.path.exists(parent):
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError as e:
                raise ToolError(_MSG_CANNOT_MKDIR.format(path=full_path, error=e))
        normalized = content
        if line_ending != "\n":
            normalized = content.replace("\r\n", "\n").replace("\n", line_ending)
        try:
            with open(full_path, "w", encoding="utf-8", newline="") as f:
                f.write(normalized)
        except OSError as e:
            raise ToolError(_MSG_CANNOT_WRITE.format(path=full_path, error=e))

    @staticmethod
    def _summary(
        added: List[str],
        updated: List[Tuple[str, str]],
        deleted: List[str],
    ) -> str:
        lines = ["Applied patch:"]
        for path in added:
            lines.append(f"  A {path}")
        for display, suffix in updated:
            lines.append(f"  M {display}{suffix}")
        for path in deleted:
            lines.append(f"  D {path}")
        counts = f"{len(added)} added, {len(updated)} updated, {len(deleted)} deleted"
        lines.append(f"({counts})")
        return "\n".join(lines)
