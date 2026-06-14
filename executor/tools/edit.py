"""Edit (update) file tool — aligned with Claude Code's Edit (FileEditTool).

Performs exact string replacements in a file: ``old_string`` is located in the
file and swapped for ``new_string`` (one occurrence by default, or every
occurrence when ``replace_all`` is set). This is the in-place counterpart to the
Write tool — prefer it when only part of a file changes.

Behavior is ported from CC's FileEditTool so model usage stays familiar:
- Read-before-edit is enforced via the Role's shared file-read state
  (Role.get_file_read_mtime): an existing file must have been read this session
  and be unchanged on disk since that read. Skipped when unbound (no Role).
- A forgiving match cascade (findActualString): exact match, then curly→straight
  quote normalization, then tab↔space normalization, then both combined. This
  recovers matches when the model copies from Read output (tabs rendered as
  spaces) or the file uses typographic quotes.
- When the match only succeeded after quote normalization, new_string's quotes
  are re-styled to the file's curly form so the edit preserves typography.
- old_string == '' creates a new file (or fills an empty one) with new_string,
  mirroring CC's create-via-edit path.
- The existing file's newline style (LF vs CRLF) is detected and preserved on
  write, the same as the Write tool.

Differences from CC, by design: no LSP/skills/analytics/git-diff/file-history
side effects, and encoding handling matches the Write tool (UTF-8) rather than
round-tripping UTF-16.
"""
from __future__ import annotations

import os
from typing import ClassVar, Optional

from metagpt.executor.tool_registry import register_tool
from metagpt.executor.tool_result import ToolError
from metagpt.executor.tools._file_base import FileMutatingTool
from metagpt.common.const.tools import MAX_EDIT_FILE_SIZE_BYTES

# Curly quotes. The model emits straight quotes; files may contain curly ones.
# We normalize curly→straight for matching, then re-apply curly on write.
_LEFT_SINGLE = "\u2018"
_RIGHT_SINGLE = "\u2019"
_LEFT_DOUBLE = "\u201c"
_RIGHT_DOUBLE = "\u201d"


def _normalize_quotes(s: str) -> str:
    """Convert curly quotes to straight quotes."""
    return (
        s.replace(_LEFT_SINGLE, "'")
        .replace(_RIGHT_SINGLE, "'")
        .replace(_LEFT_DOUBLE, '"')
        .replace(_RIGHT_DOUBLE, '"')
    )


def _normalize_whitespace(s: str) -> str:
    """Expand tabs to 4 spaces (Read output renders tabs as spaces)."""
    return s.replace("\t", "    ")


def _map_back(file_content: str, normalized_file: str, norm_start: int, norm_len: int) -> str:
    """Map a match in a tab-expanded view back to the original file substring."""
    norm_pos = 0
    orig_pos = 0
    orig_start = -1
    orig_end = -1
    n = len(file_content)
    target_end = norm_start + norm_len

    while orig_pos < n and norm_pos <= target_end:
        if norm_pos == norm_start:
            orig_start = orig_pos
        if norm_pos == target_end:
            orig_end = orig_pos
            break

        ch = file_content[orig_pos]
        if ch == "\t":
            next_norm = norm_pos + 4
            if norm_pos < norm_start < next_norm and orig_start == -1:
                orig_start = orig_pos
            if norm_pos < target_end < next_norm and orig_end == -1:
                orig_end = orig_pos + 1
            norm_pos = next_norm
            orig_pos += 1
        else:
            norm_pos += 1
            orig_pos += 1

    if orig_start == -1:
        orig_start = 0
    if orig_end == -1:
        ratio = n / len(normalized_file) if normalized_file else 1
        orig_end = round(orig_start + norm_len * ratio)
    return file_content[orig_start:orig_end]


def _find_actual_string(file_content: str, search: str) -> Optional[str]:
    """Find the substring in file_content matching `search`, tolerating quote and
    tab/space differences. Returns the actual matched substring, or None.
    """
    # 1. Exact match.
    if search in file_content:
        return search

    # 2. Quote normalization.
    norm_search = _normalize_quotes(search)
    norm_file = _normalize_quotes(file_content)
    idx = norm_file.find(norm_search)
    if idx != -1:
        return file_content[idx : idx + len(search)]

    # 3. Tab/space normalization.
    ws_file = _normalize_whitespace(file_content)
    ws_search = _normalize_whitespace(search)
    ws_idx = ws_file.find(ws_search)
    if ws_idx != -1:
        return _map_back(file_content, ws_file, ws_idx, len(ws_search))

    # 4. Quote + tab/space normalization combined.
    combined_file = _normalize_whitespace(norm_file)
    combined_search = _normalize_whitespace(norm_search)
    c_idx = combined_file.find(combined_search)
    if c_idx != -1:
        return _map_back(file_content, combined_file, c_idx, len(combined_search))

    return None


def _is_opening_context(chars: str, i: int) -> bool:
    if i == 0:
        return True
    prev = chars[i - 1]
    return prev in (" ", "\t", "\n", "\r", "(", "[", "{", "\u2014", "\u2013")


def _apply_curly_double(s: str) -> str:
    out = []
    for i, ch in enumerate(s):
        if ch == '"':
            out.append(_LEFT_DOUBLE if _is_opening_context(s, i) else _RIGHT_DOUBLE)
        else:
            out.append(ch)
    return "".join(out)


def _apply_curly_single(s: str) -> str:
    out = []
    for i, ch in enumerate(s):
        if ch == "'":
            prev = s[i - 1] if i > 0 else None
            nxt = s[i + 1] if i < len(s) - 1 else None
            prev_letter = prev is not None and prev.isalpha()
            next_letter = nxt is not None and nxt.isalpha()
            if prev_letter and next_letter:
                out.append(_RIGHT_SINGLE)  # contraction apostrophe
            else:
                out.append(_LEFT_SINGLE if _is_opening_context(s, i) else _RIGHT_SINGLE)
        else:
            out.append(ch)
    return "".join(out)


def _preserve_quote_style(old_string: str, actual_old: str, new_string: str) -> str:
    """If matching curly-normalized old_string, re-style new_string's quotes to
    the file's curly form so the edit preserves typography.
    """
    if old_string == actual_old:
        return new_string
    has_double = _LEFT_DOUBLE in actual_old or _RIGHT_DOUBLE in actual_old
    has_single = _LEFT_SINGLE in actual_old or _RIGHT_SINGLE in actual_old
    result = new_string
    if has_double:
        result = _apply_curly_double(result)
    if has_single:
        result = _apply_curly_single(result)
    return result


def _apply_edit(content: str, old_string: str, new_string: str, replace_all: bool) -> str:
    """Replace old_string with new_string in content.

    When deleting (new_string == '') and old_string doesn't end with a newline
    but is followed by one in the file, the trailing newline is consumed too so
    deletion doesn't leave a blank line (mirrors CC's applyEditToFile).
    """
    count = -1 if replace_all else 1
    if new_string != "":
        return content.replace(old_string, new_string, count)

    if not old_string.endswith("\n") and (old_string + "\n") in content:
        return content.replace(old_string + "\n", "", count)
    return content.replace(old_string, "", count)


@register_tool
class Edit(FileMutatingTool):
    """Perform an exact string replacement in a file (the update tool)."""

    name = "Edit"
    aliases: ClassVar[list[str]] = ["Edit.run", "edit", "Update", "update"]
    # Success messages can echo a code snippet; allow a higher cap (CC).
    max_result_size_chars: ClassVar[int] = 100_000
    description = (
        "Performs exact string replacements in files. You must Read the file at "
        "least once before editing it. The edit fails if old_string is not unique "
        "in the file — provide more surrounding context to make it unique, or set "
        "replace_all to replace every occurrence (useful for renaming)."
    )

    async def call(
        self,
        *,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> str:
        """Perform an exact string replacement in a file.

        Locates old_string in the file and replaces it with new_string. By
        default exactly one occurrence is replaced and old_string must be unique;
        set replace_all to replace every occurrence. Pass an empty old_string to
        create a new file (or fill an empty one) with new_string as its content.

        Args:
            file_path: Absolute path to the file to modify (~ is expanded;
                relative paths resolve against the current working directory).
            old_string: The text to replace. Must match the file exactly
                (including indentation) and be unique unless replace_all is set.
                Empty string means "create the file with new_string".
            new_string: The replacement text. Must differ from old_string.
            replace_all: Replace all occurrences of old_string (default False).
        """
        if not file_path or not file_path.strip():
            raise ToolError("Error: 'file_path' argument is required.")
        if old_string is None or new_string is None:
            raise ToolError("Error: 'old_string' and 'new_string' are required.")
        if not isinstance(old_string, str) or not isinstance(new_string, str):
            raise ToolError("Error: 'old_string' and 'new_string' must be strings.")
        if old_string == new_string:
            raise ToolError("Error: no changes to make — old_string and new_string are identical.")

        full_path = os.path.abspath(os.path.expanduser(file_path.strip()))

        if os.path.isdir(full_path):
            raise ToolError(f"Error: '{file_path}' is a directory, not a file.")
        if full_path.endswith(".ipynb"):
            raise ToolError(
                f"Error: '{file_path}' is a Jupyter notebook. Use a notebook edit "
                f"tool to modify .ipynb files."
            )

        existed = os.path.exists(full_path)

        # --- New-file creation path: empty old_string ---
        if old_string == "":
            return self._create_file(file_path, full_path, new_string, existed)

        if not existed:
            raise ToolError(
                f"Error: file does not exist. Note that the path should be "
                f"absolute; the current working directory is {os.getcwd()}. To "
                f"create a new file, pass an empty old_string."
            )

        # Size guard before reading the whole file into memory.
        try:
            size = os.stat(full_path).st_size
        except OSError as e:
            raise ToolError(f"Error: cannot stat '{file_path}': {e}")
        if size > MAX_EDIT_FILE_SIZE_BYTES:
            raise ToolError(
                f"Error: file ({size} bytes) exceeds the maximum editable size "
                f"({MAX_EDIT_FILE_SIZE_BYTES} bytes)."
            )

        # Read-before-edit + unchanged-since-read guard (raises ToolError to abort).
        self._check_read_before_write(
            file_path, full_path, noun="file", verb="editing"
        )

        line_ending = self._detect_line_ending(full_path)

        try:
            # newline="" disables Python translation; we normalize to "\n" so
            # matching is line-ending agnostic, then translate back on write.
            with open(full_path, "r", encoding="utf-8", newline="") as f:
                raw = f.read()
        except UnicodeDecodeError:
            raise ToolError(f"Error: cannot edit '{file_path}': file is not valid UTF-8 text.")
        except OSError as e:
            raise ToolError(f"Error: cannot read '{file_path}': {e}")

        content = raw.replace("\r\n", "\n")

        actual_old = _find_actual_string(content, old_string)
        if actual_old is None:
            raise ToolError(
                f"Error: string to replace not found in file.\nString: {old_string}"
            )

        matches = content.count(actual_old)
        if matches > 1 and not replace_all:
            raise ToolError(
                f"Error: found {matches} matches of the string to replace, but "
                f"replace_all is false. To replace all occurrences set replace_all "
                f"to true. To replace only one, provide more surrounding context to "
                f"uniquely identify the instance.\nString: {old_string}"
            )

        actual_new = _preserve_quote_style(old_string, actual_old, new_string)
        updated = _apply_edit(content, actual_old, actual_new, replace_all)

        if updated == content:
            raise ToolError("Error: applying the edit produced no change to the file.")

        normalized = updated
        if line_ending != "\n":
            normalized = updated.replace("\n", line_ending)
        try:
            with open(full_path, "w", encoding="utf-8", newline="") as f:
                f.write(normalized)
        except OSError as e:
            raise ToolError(f"Error: cannot write '{file_path}': {e}")

        self._refresh_read_state(full_path)

        if replace_all:
            return (
                f"The file {full_path} has been updated. All {matches} "
                f"occurrence(s) were successfully replaced."
            )
        return f"The file {full_path} has been updated successfully."

    def _create_file(self, file_path: str, full_path: str, content: str, existed: bool) -> str:
        """Handle the empty-old_string create path.

        Valid only when the file doesn't exist, or exists but is empty/whitespace
        (mirrors CC's create-via-edit). Otherwise refuses to clobber content.
        """
        if existed:
            try:
                with open(full_path, "r", encoding="utf-8", newline="") as f:
                    current = f.read()
            except (OSError, UnicodeDecodeError):
                current = "non-empty"  # treat unreadable as non-empty → refuse
            if current.strip() != "":
                raise ToolError(
                    f"Error: cannot create new file — '{file_path}' already exists "
                    f"with content. Provide a non-empty old_string to edit it."
                )

        parent = os.path.dirname(full_path)
        if parent and not os.path.exists(parent):
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError as e:
                raise ToolError(f"Error: cannot create parent directory for '{file_path}': {e}")

        try:
            with open(full_path, "w", encoding="utf-8", newline="") as f:
                f.write(content)
        except OSError as e:
            raise ToolError(f"Error: cannot write '{file_path}': {e}")

        self._refresh_read_state(full_path)
        verb = "updated" if existed else "created"
        return f"The file {full_path} has been {verb} successfully."

