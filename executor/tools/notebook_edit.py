"""NotebookEdit tool — aligned with Claude Code's NotebookEdit (NotebookEditTool).

Edits a single cell of a Jupyter notebook (.ipynb) in place. The three edit
modes mirror CC's tool:
- ``replace`` (default): overwrite the target cell's source with ``new_source``.
  For a code cell, ``execution_count`` is reset to null and ``outputs`` cleared,
  since the cell changed. Optionally retypes the cell when ``cell_type`` differs.
- ``insert``: add a brand-new cell. It is placed *after* the cell named by
  ``cell_id`` (or at the very beginning when no ``cell_id`` is given).
  ``cell_type`` is required so the new cell is well-formed.
- ``delete``: remove the cell named by ``cell_id``.

Cell addressing matches CC: ``cell_id`` is matched first against each cell's
real ``id`` field; failing that it is parsed as the ``cell-N`` positional form
(0-indexed). A ``replace`` aimed one past the end is promoted to ``insert``.

Read-before-edit is enforced the same way as the Edit/Write tools, through the
Role's shared file-read state (Role.get_file_read_mtime): the notebook must have
been read this session and be unchanged on disk since. Both guards are skipped
when the tool is unbound (no Role), so it still works standalone/in tests.

Differences from CC, by design: no permissions/file-history/analytics side
effects, and the notebook is written back as UTF-8 with indent=1 (matching CC's
IPYNB_INDENT) while preserving the file's existing newline style.
"""
from __future__ import annotations

import json
import os
import random
import re
import string
from typing import Any, ClassVar, Optional

from metagpt.executor.tool_registry import register_tool
from metagpt.executor.tool_result import ToolError
from metagpt.executor.tools._file_base import FileMutatingTool
from metagpt.common.const.tools import MAX_NOTEBOOK_SIZE_BYTES

# JSON indent CC uses when writing .ipynb back (IPYNB_INDENT).
_IPYNB_INDENT = 1

# Matches the positional "cell-N" id form (0-indexed).
_CELL_ID_RE = re.compile(r"^cell-(\d+)$")


def _parse_cell_id(cell_id: str) -> Optional[int]:
    """Parse the positional "cell-N" form into its 0-indexed integer, else None."""
    match = _CELL_ID_RE.match(cell_id)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    return None


def _generate_cell_id() -> str:
    """Generate a short random cell id (mirrors CC's base36 random id)."""
    alphabet = string.digits + string.ascii_lowercase
    return "".join(random.choices(alphabet, k=13))


def _supports_cell_ids(notebook: dict) -> bool:
    """nbformat >= 4.5 carries per-cell ids (CC's nbformat check)."""
    nbformat = notebook.get("nbformat", 4)
    nbformat_minor = notebook.get("nbformat_minor", 0)
    return nbformat > 4 or (nbformat == 4 and nbformat_minor >= 5)


def _find_cell_index(cells: list, cell_id: str) -> Optional[int]:
    """Resolve a cell_id to an index: real ``id`` first, then ``cell-N`` form.

    Returns the index, or None when it can't be resolved at all.
    """
    for i, cell in enumerate(cells):
        if isinstance(cell, dict) and cell.get("id") == cell_id:
            return i
    parsed = _parse_cell_id(cell_id)
    if parsed is not None:
        return parsed
    return None


@register_tool
class NotebookEdit(FileMutatingTool):
    """Replace, insert, or delete a single cell in a Jupyter notebook (.ipynb)."""

    name = "NotebookEdit"
    aliases: ClassVar[list[str]] = ["NotebookEdit.run", "notebook_edit"]
    description = (
        "Completely replaces the contents of a specific cell in a Jupyter "
        "notebook (.ipynb file) with new source. The notebook_path must be "
        "absolute. Use edit_mode=insert to add a new cell after the cell named "
        "by cell_id (or at the start if omitted), and edit_mode=delete to "
        "remove the cell named by cell_id."
    )

    async def call(
        self,
        *,
        notebook_path: str,
        new_source: str,
        cell_id: Optional[str] = None,
        cell_type: Optional[str] = None,
        edit_mode: str = "replace",
    ) -> str:
        """Edit a single cell of a Jupyter notebook.

        Args:
            notebook_path: Absolute path to the .ipynb file to edit (~ is
                expanded; relative paths resolve against the current working
                directory).
            new_source: The new source for the cell. Ignored for delete.
            cell_id: The id of the cell to edit. For insert, the new cell is
                placed after this cell (or at the start if omitted). Matched
                against each cell's real id first, then the positional "cell-N"
                form. Required unless edit_mode=insert.
            cell_type: The cell type, "code" or "markdown". Defaults to the
                target cell's current type for replace; required for insert.
            edit_mode: One of "replace" (default), "insert", or "delete".
        """
        if not notebook_path or not notebook_path.strip():
            raise ToolError("Error: 'notebook_path' argument is required.")
        if new_source is None:
            new_source = ""
        if not isinstance(new_source, str):
            raise ToolError("Error: 'new_source' must be a string.")
        if edit_mode not in ("replace", "insert", "delete"):
            raise ToolError("Error: edit_mode must be one of 'replace', 'insert', or 'delete'.")
        if cell_type is not None and cell_type not in ("code", "markdown"):
            raise ToolError("Error: cell_type must be 'code' or 'markdown'.")
        if edit_mode == "insert" and not cell_type:
            raise ToolError("Error: cell_type is required when using edit_mode=insert.")

        full_path = os.path.abspath(os.path.expanduser(notebook_path.strip()))

        if os.path.isdir(full_path):
            raise ToolError(f"Error: '{notebook_path}' is a directory, not a file.")
        if not full_path.endswith(".ipynb"):
            raise ToolError(
                f"Error: '{notebook_path}' is not a Jupyter notebook. This tool "
                f"only edits .ipynb files; use the Edit tool for other files."
            )
        if not os.path.exists(full_path):
            raise ToolError(
                f"Error: notebook does not exist. Note that the path should be "
                f"absolute; the current working directory is {os.getcwd()}."
            )

        # Size guard before reading the whole notebook into memory.
        try:
            size = os.stat(full_path).st_size
        except OSError as e:
            raise ToolError(f"Error: cannot stat '{notebook_path}': {e}")
        if size > MAX_NOTEBOOK_SIZE_BYTES:
            raise ToolError(
                f"Error: notebook ({size} bytes) exceeds the maximum editable "
                f"size ({MAX_NOTEBOOK_SIZE_BYTES} bytes)."
            )

        # Read-before-edit + unchanged-since-read guard (raises ToolError to abort).
        self._check_read_before_write(
            notebook_path, full_path, noun="notebook", verb="editing"
        )

        line_ending = self._detect_line_ending(full_path)

        try:
            with open(full_path, "r", encoding="utf-8", newline="") as f:
                raw = f.read()
        except UnicodeDecodeError:
            raise ToolError(f"Error: cannot edit '{notebook_path}': file is not valid UTF-8 text.")
        except OSError as e:
            raise ToolError(f"Error: cannot read '{notebook_path}': {e}")

        try:
            notebook = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ToolError(f"Error: '{notebook_path}' is not a valid notebook (invalid JSON): {e}")
        if not isinstance(notebook, dict) or not isinstance(notebook.get("cells"), list):
            raise ToolError(f"Error: '{notebook_path}' is not a valid notebook (missing cells).")

        cells = notebook["cells"]

        # --- Resolve the target cell index (mirrors CC). ---
        if not cell_id:
            if edit_mode != "insert":
                raise ToolError("Error: cell_id must be specified when not inserting a new cell.")
            cell_index = 0
        else:
            resolved = _find_cell_index(cells, cell_id)
            if resolved is None:
                raise ToolError(f'Error: cell with id "{cell_id}" not found in notebook.')
            cell_index = resolved
            if edit_mode == "insert":
                cell_index += 1  # insert after the named cell

        # Replace one past the end is promoted to an insert (CC behavior).
        effective_mode = edit_mode
        if effective_mode == "replace" and cell_index == len(cells):
            effective_mode = "insert"
            if not cell_type:
                cell_type = "code"

        # Bounds check for operations that target an existing cell.
        if effective_mode in ("replace", "delete"):
            if cell_index < 0 or cell_index >= len(cells):
                raise ToolError(f"Error: cell index {cell_index} does not exist in notebook.")
        elif effective_mode == "insert":
            if cell_index < 0 or cell_index > len(cells):
                raise ToolError(f"Error: cannot insert at index {cell_index}; out of range.")

        # --- Apply the edit. ---
        result_cell_id = cell_id
        if effective_mode == "delete":
            del cells[cell_index]
        elif effective_mode == "insert":
            new_cell_id = _generate_cell_id() if _supports_cell_ids(notebook) else None
            cells.insert(cell_index, self._build_cell(cell_type, new_source, new_cell_id))
            result_cell_id = new_cell_id
        else:  # replace
            target = cells[cell_index]
            target["source"] = new_source
            if target.get("cell_type") == "code":
                target["execution_count"] = None
                target["outputs"] = []
            if cell_type and cell_type != target.get("cell_type"):
                target["cell_type"] = cell_type
            result_cell_id = target.get("id", cell_id)

        # --- Write the notebook back, preserving newline style. ---
        try:
            updated = json.dumps(notebook, indent=_IPYNB_INDENT, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            raise ToolError(f"Error: cannot serialize notebook '{notebook_path}': {e}")
        if line_ending != "\n":
            updated = updated.replace("\n", line_ending)
        try:
            with open(full_path, "w", encoding="utf-8", newline="") as f:
                f.write(updated)
        except OSError as e:
            raise ToolError(f"Error: cannot write '{notebook_path}': {e}")

        self._refresh_read_state(full_path)

        cell_ref = result_cell_id if result_cell_id is not None else f"index {cell_index}"
        if effective_mode == "delete":
            return f"Deleted cell {cell_ref} in {full_path}."
        if effective_mode == "insert":
            return f"Inserted a new {cell_type} cell ({cell_ref}) in {full_path}."
        return f"Updated cell {cell_ref} in {full_path}."

    def _build_cell(self, cell_type: str, source: str, cell_id: Optional[str]) -> dict:
        """Build a fresh, well-formed notebook cell."""
        cell: dict[str, Any] = {"cell_type": cell_type, "metadata": {}, "source": source}
        if cell_id is not None:
            cell["id"] = cell_id
        if cell_type == "code":
            cell["execution_count"] = None
            cell["outputs"] = []
        return cell

