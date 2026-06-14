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

from metagpt.common.const.tools import MAX_CONTENT_SIZE_BYTES
from metagpt.executor.dependency._apply_patch import (
    AddFile,
    ApplyPatchError,
    DeleteFile,
    UpdateFile,
    affected_paths,
    apply_update,
    parse_patch,
)
from metagpt.executor.dependency._file_base import FileMutatingTool
from metagpt.executor.tool_registry import register_tool
from metagpt.executor.tool_result import ToolError

# The patch grammar spec, embedded in the tool description so the model learns
# the exact format on both channels.
_GRAMMAR_SPEC = """\
Apply a structured patch that can Add, Update, Delete, and Move/rename multiple \
files in a single call. Pass the whole patch as the single `input` string, in \
this format:

*** Begin Patch
*** Add File: path/to/new_file.py
+line one of the new file
+line two
*** Delete File: path/to/remove_me.py
*** Update File: path/to/edit_me.py
*** Move to: path/to/renamed.py
@@ optional context anchor (e.g. a function or class signature)
 a context line that already exists (note the single leading space)
-a line to remove
+a line to add
*** End Patch

Rules:
- The patch MUST start with `*** Begin Patch` and end with `*** End Patch`.
- Inside an `*** Update File` hunk, every line starts with one of: a single \
space (unchanged context), `+` (added line), or `-` (removed line). A `@@` line \
optionally followed by a one-line context anchor begins a new chunk and helps \
locate the edit. You do NOT use line numbers; provide a few surrounding context \
lines so the edit can be located unambiguously.
- `*** Move to:` (Update only) renames the file as it is updated.
- `*** End of File` marks a chunk as anchored to the end of the file.
- You must Read a file before Updating, Deleting, or Moving it.
"""


@register_tool
class ApplyPatch(FileMutatingTool):
    """Apply a multi-file Add/Update/Delete/Move patch in codex's patch format."""

    name = "ApplyPatch"
    aliases: ClassVar[list[str]] = ["apply_patch", "ApplyPatch.run", "applyPatch"]
    # Summaries can echo many paths; allow a higher cap like Edit/Write.
    max_result_size_chars: ClassVar[int] = 100_000
    description = _GRAMMAR_SPEC

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

    async def call(self, *, input: str) -> str:
        """Apply a structured multi-file patch.

        Args:
            input: The full patch text, beginning with ``*** Begin Patch`` and
                ending with ``*** End Patch`` (see the tool description for the
                grammar). Add / Update / Delete / Move operations are applied
                transactionally — if any context anchor cannot be located, the
                whole patch is rejected with no files written.
        """
        if not isinstance(input, str) or not input.strip():
            raise ToolError("Error: 'input' (the patch text) is required.")

        try:
            hunks = parse_patch(input)
        except ApplyPatchError as e:
            raise ToolError(f"Error: failed to parse patch: {e}")

        if not hunks:
            raise ToolError("Error: the patch contained no file operations.")

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
        for plan in plans:
            op = plan["op"]
            if op == "add":
                self._do_write(plan["full"], plan["content"], "\n")
                self._refresh_read_state(plan["full"])
                added.append(plan["display"])
            elif op == "delete":
                self._snapshot_pre_write(plan["full"])
                try:
                    os.unlink(plan["full"])
                except OSError as e:
                    raise ToolError(f"Error: cannot delete '{plan['display']}': {e}")
                deleted.append(plan["display"])
            elif op == "update":
                self._snapshot_pre_write(plan["src"])
                if plan["moved"]:
                    self._do_write(plan["dest"], plan["content"], plan["line_ending"])
                    try:
                        os.unlink(plan["src"])
                    except OSError as e:
                        raise ToolError(
                            f"Error: cannot remove original '{plan['display']}': {e}"
                        )
                    self._refresh_read_state(plan["dest"])
                    updated.append((plan["display"], f" -> {plan['move_display']}"))
                else:
                    self._do_write(plan["dest"], plan["content"], plan["line_ending"])
                    self._refresh_read_state(plan["dest"])
                    updated.append((plan["display"], ""))

        return self._summary(added, updated, deleted)

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
                raise ToolError(
                    f"Error: cannot add '{hunk.path}': a file already exists there "
                    f"with content. Use an Update File hunk to edit it."
                )
        self._guard_size(hunk.path, hunk.contents)
        return {"op": "add", "display": hunk.path, "full": full, "content": hunk.contents}

    def _plan_delete(self, hunk: DeleteFile) -> dict:
        full = self._resolve(hunk.path)
        self._reject_special(hunk.path, full)
        if not os.path.exists(full):
            raise ToolError(f"Error: cannot delete '{hunk.path}': file does not exist.")
        self._check_read_before_write(hunk.path, full, verb="deleting")
        return {"op": "delete", "display": hunk.path, "full": full}

    def _plan_update(self, hunk: UpdateFile) -> dict:
        src = self._resolve(hunk.path)
        self._reject_special(hunk.path, src)
        if not os.path.exists(src):
            raise ToolError(f"Error: cannot update '{hunk.path}': file does not exist.")
        self._check_read_before_write(hunk.path, src, verb="editing")

        dest = src
        moved = False
        move_display = ""
        if hunk.move_path is not None:
            dest = self._resolve(hunk.move_path)
            self._reject_special(hunk.move_path, dest)
            if os.path.exists(dest):
                raise ToolError(
                    f"Error: cannot move '{hunk.path}' to '{hunk.move_path}': "
                    f"the destination already exists."
                )
            moved = True
            move_display = hunk.move_path

        line_ending = self._detect_line_ending(src)
        try:
            with open(src, "r", encoding="utf-8", newline="") as f:
                raw = f.read()
        except UnicodeDecodeError:
            raise ToolError(f"Error: cannot update '{hunk.path}': file is not valid UTF-8 text.")
        except OSError as e:
            raise ToolError(f"Error: cannot read '{hunk.path}': {e}")

        content = raw.replace("\r\n", "\n")
        try:
            new_content = apply_update(content, hunk.chunks)
        except ApplyPatchError as e:
            raise ToolError(f"Error: cannot apply update to '{hunk.path}': {e}")

        self._guard_size(hunk.path, new_content)
        return {
            "op": "update",
            "display": hunk.path,
            "src": src,
            "dest": dest,
            "moved": moved,
            "move_display": move_display,
            "content": new_content,
            "line_ending": line_ending,
        }

    # ------------------------------------------------------------------
    # Shared low-level helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve(path: str) -> str:
        return os.path.abspath(os.path.expanduser(path.strip()))

    @staticmethod
    def _reject_special(display_path: str, full_path: str) -> None:
        if os.path.isdir(full_path):
            raise ToolError(f"Error: '{display_path}' is a directory, not a file.")
        if full_path.endswith(".ipynb"):
            raise ToolError(
                f"Error: '{display_path}' is a Jupyter notebook. Use a notebook "
                f"edit tool to modify .ipynb files."
            )

    @staticmethod
    def _guard_size(display_path: str, content: str) -> None:
        size = len(content.encode("utf-8"))
        if size > MAX_CONTENT_SIZE_BYTES:
            raise ToolError(
                f"Error: resulting content for '{display_path}' ({size} bytes) "
                f"exceeds the maximum allowed size ({MAX_CONTENT_SIZE_BYTES} bytes)."
            )

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
                raise ToolError(f"Error: cannot create parent directory for '{full_path}': {e}")
        normalized = content
        if line_ending != "\n":
            normalized = content.replace("\r\n", "\n").replace("\n", line_ending)
        try:
            with open(full_path, "w", encoding="utf-8", newline="") as f:
                f.write(normalized)
        except OSError as e:
            raise ToolError(f"Error: cannot write '{full_path}': {e}")

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
        counts = (
            f"{len(added)} added, {len(updated)} updated, {len(deleted)} deleted"
        )
        lines.append(f"({counts})")
        return "\n".join(lines)
