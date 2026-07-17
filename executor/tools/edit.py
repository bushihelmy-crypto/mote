"""Edit (update) file tool.

Performs exact string replacements in a file: ``old_string`` is located in the
file and swapped for ``new_string`` (one occurrence by default, or every
occurrence when ``replace_all`` is set). This is the in-place counterpart to the
Write tool — prefer it when only part of a file changes.

Behavior:
- Read-before-edit is enforced via the Role's shared file-read state
  (Role.get_file_read_mtime): an existing file must have been read this session
  and be unchanged on disk since that read. Skipped when unbound (no Role).
- A forgiving match cascade: exact match, then curly→straight
  quote normalization, then tab↔space normalization, then both combined. This
  recovers matches when the model copies from Read output (tabs rendered as
  spaces) or the file uses typographic quotes.
- When the match only succeeded after quote normalization, new_string's quotes
  are re-styled to the file's curly form so the edit preserves typography.
- old_string == '' creates a new file (or fills an empty one) with new_string,
  via the create-via-edit path.
- The existing file's newline style (LF vs CRLF) is detected and preserved on
  write, the same as the Write tool.

Differences by design: no LSP/skills/analytics/git-diff/file-history
side effects, and encoding handling matches the Write tool (UTF-8) rather than
round-tripping UTF-16.
"""
from __future__ import annotations

import os
from typing import ClassVar, Optional

from mote.common.const.tools import MAX_EDIT_FILE_SIZE_BYTES
from mote.common.text import count_noun, verb_agree
from mote.executor.dependency._file_base import FileMutatingTool
from mote.executor.tool_registry import register_tool
from mote.executor.tool_result import FileChange, ToolError, ToolResult

# Complete model-facing message sentences, hoisted to module-top templates so the
# wording lives in one place (fill via ``.format(...)`` at the raise/return site).
_MSG_FILE_PATH_REQUIRED = "Error: 'file_path' argument is required."
_MSG_STRINGS_REQUIRED = "Error: 'old_string' and 'new_string' are required."
_MSG_STRINGS_MUST_BE_STR = "Error: 'old_string' and 'new_string' must be strings."
_MSG_NO_CHANGES = "Error: no changes to make — old_string and new_string are identical."
_MSG_IS_DIRECTORY = "Error: '{path}' is a directory, not a file."
_MSG_IS_NOTEBOOK = "Error: '{path}' is a Jupyter notebook. Use a notebook edit tool to modify " ".ipynb files."
_MSG_FILE_NOT_EXIST = (
    "Error: file does not exist. Note that relative paths resolve against the "
    "working directory {base}. To create a new file, pass an empty old_string."
)
_MSG_CANNOT_STAT = "Error: cannot stat '{path}': {error}"
_MSG_FILE_TOO_LARGE = "Error: file ({size} bytes) exceeds the maximum editable size ({max_size} bytes)."
_MSG_NOT_UTF8 = "Error: cannot edit '{path}': file is not valid UTF-8 text."
_MSG_CANNOT_READ = "Error: cannot read '{path}': {error}"
_MSG_NOT_FOUND = "Error: string to replace not found in file.\nString: {old_string}"
_MSG_MULTIPLE_MATCHES = (
    "Error: found {count} matches of the string to replace, but replace_all is "
    "false. To replace all occurrences set replace_all to true. To replace only "
    "one, provide more surrounding context to uniquely identify the instance."
    "\nString: {old_string}"
)
_MSG_NO_CHANGE_PRODUCED = "Error: applying the edit produced no change to the file."
_MSG_CANNOT_WRITE = "Error: cannot write '{path}': {error}"
_MSG_UPDATED_ALL = "The file {path} has been updated. All {count} {verb} successfully replaced."
_MSG_UPDATED = "The file {path} has been updated successfully."
_MSG_CANNOT_CLOBBER = (
    "Error: cannot create new file — '{path}' already exists with content. "
    "Provide a non-empty old_string to edit it."
)
_MSG_CANNOT_MKDIR = "Error: cannot create parent directory for '{path}': {error}"
_MSG_CREATED = "The file {path} has been {verb} successfully."

# Curly quotes. The model emits straight quotes; files may contain curly ones.
# We normalize curly→straight for matching, then re-apply curly on write.
_LEFT_SINGLE = "\u2018"
_RIGHT_SINGLE = "\u2019"
_LEFT_DOUBLE = "\u201c"
_RIGHT_DOUBLE = "\u201d"


def _normalize_quotes(s: str) -> str:
    """Convert curly quotes to straight quotes."""
    return (
        s.replace(_LEFT_SINGLE, "'").replace(_RIGHT_SINGLE, "'").replace(_LEFT_DOUBLE, '"').replace(_RIGHT_DOUBLE, '"')
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
    deletion doesn't leave a blank line.
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
    # The effect (edited file) is durable and re-readable, so the success-message
    # body can be cleared without losing recoverable information.
    reconstructable: ClassVar[bool] = True
    # Success messages can echo a code snippet; allow a higher cap.
    max_result_size_chars: ClassVar[int] = 100_000

    async def call(
        self,
        *,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> ToolResult:
        """Edit a file by replacing an exact string — precise in-place changes.

        Performs exact string replacements in files. Locates old_string in the
        file and replaces it with new_string. By default exactly one occurrence
        is replaced and old_string must be unique; set replace_all to replace
        every occurrence. Pass an empty old_string to create a new file (or fill
        an empty one) with new_string as its content.

        You must use the Read tool at least once on the file before editing it —
        the edit fails otherwise. Preserve the exact indentation (tabs/spaces) as
        it appears in the file, but do NOT include the line-number prefix that
        Read adds to its output.

        The edit fails if old_string is not unique in the file: either add more
        surrounding context so the match is unique, or set replace_all=true to
        change every occurrence (useful for renaming a variable across the file).
        Prefer editing an existing file over rewriting it whole with Write.

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
            raise ToolError(_MSG_FILE_PATH_REQUIRED)
        if old_string is None or new_string is None:
            raise ToolError(_MSG_STRINGS_REQUIRED)
        if not isinstance(old_string, str) or not isinstance(new_string, str):
            raise ToolError(_MSG_STRINGS_MUST_BE_STR)
        if old_string == new_string:
            raise ToolError(_MSG_NO_CHANGES)

        full_path = self._resolve_path(file_path.strip())

        if os.path.isdir(full_path):
            raise ToolError(_MSG_IS_DIRECTORY.format(path=file_path))
        if full_path.endswith(".ipynb"):
            raise ToolError(_MSG_IS_NOTEBOOK.format(path=file_path))

        existed = os.path.exists(full_path)

        # --- New-file creation path: empty old_string ---
        if old_string == "":
            return self._create_file(file_path, full_path, new_string, existed)

        if not existed:
            raise ToolError(_MSG_FILE_NOT_EXIST.format(base=self._base_cwd()))

        # Size guard before reading the whole file into memory.
        try:
            size = os.stat(full_path).st_size
        except OSError as e:
            raise ToolError(_MSG_CANNOT_STAT.format(path=file_path, error=e))
        if size > MAX_EDIT_FILE_SIZE_BYTES:
            raise ToolError(_MSG_FILE_TOO_LARGE.format(size=size, max_size=MAX_EDIT_FILE_SIZE_BYTES))

        # Read-before-edit + unchanged-since-read guard (raises ToolError to abort).
        self._check_read_before_write(file_path, full_path, noun="file", verb="editing")

        line_ending = self._detect_line_ending(full_path)

        try:
            # newline="" disables Python translation; we normalize to "\n" so
            # matching is line-ending agnostic, then translate back on write.
            with open(full_path, "r", encoding="utf-8", newline="") as f:
                raw = f.read()
        except UnicodeDecodeError:
            raise ToolError(_MSG_NOT_UTF8.format(path=file_path))
        except OSError as e:
            raise ToolError(_MSG_CANNOT_READ.format(path=file_path, error=e))

        content = raw.replace("\r\n", "\n")

        actual_old = _find_actual_string(content, old_string)
        if actual_old is None:
            raise ToolError(_MSG_NOT_FOUND.format(old_string=old_string))

        matches = content.count(actual_old)
        if matches > 1 and not replace_all:
            raise ToolError(_MSG_MULTIPLE_MATCHES.format(count=matches, old_string=old_string))

        actual_new = _preserve_quote_style(old_string, actual_old, new_string)
        updated = _apply_edit(content, actual_old, actual_new, replace_all)

        if updated == content:
            raise ToolError(_MSG_NO_CHANGE_PRODUCED)

        normalized = updated
        if line_ending != "\n":
            normalized = updated.replace("\n", line_ending)
        # Capture a before-image for file history just before we overwrite.
        self._snapshot_pre_write(full_path)
        try:
            with open(full_path, "w", encoding="utf-8", newline="") as f:
                f.write(normalized)
        except OSError as e:
            raise ToolError(_MSG_CANNOT_WRITE.format(path=file_path, error=e))

        self._refresh_read_state(full_path)

        if replace_all:
            verb = verb_agree(matches, "was", "were")
            message = _MSG_UPDATED_ALL.format(path=full_path, count=count_noun(matches, "occurrence"), verb=verb)
        else:
            message = _MSG_UPDATED.format(path=full_path)
        # Carry the change as a structured fact (old/new full content, LF-normalized
        # — the display-agnostic form) so the view layer renders it without sniffing.
        return ToolResult(
            output=message,
            file_changes=[FileChange(path=full_path, old=content, new=updated)],
        )

    def _create_file(self, file_path: str, full_path: str, content: str, existed: bool) -> ToolResult:
        """Handle the empty-old_string create path.

        Valid only when the file doesn't exist, or exists but is empty/whitespace
        (the create-via-edit path). Otherwise refuses to clobber content.
        """
        old = ""
        if existed:
            try:
                with open(full_path, "r", encoding="utf-8", newline="") as f:
                    current = f.read()
            except (OSError, UnicodeDecodeError):
                current = "non-empty"  # treat unreadable as non-empty → refuse
            if current.strip() != "":
                raise ToolError(_MSG_CANNOT_CLOBBER.format(path=file_path))
            old = current.replace("\r\n", "\n")

        parent = os.path.dirname(full_path)
        if parent and not os.path.exists(parent):
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError as e:
                raise ToolError(_MSG_CANNOT_MKDIR.format(path=file_path, error=e))

        # Capture a before-image for file history just before we write.
        self._snapshot_pre_write(full_path)
        try:
            with open(full_path, "w", encoding="utf-8", newline="") as f:
                f.write(content)
        except OSError as e:
            raise ToolError(_MSG_CANNOT_WRITE.format(path=file_path, error=e))

        self._refresh_read_state(full_path)
        verb = "updated" if existed else "created"
        # ``new`` is LF-normalized to match the update path's convention (the
        # written bytes may carry the detected line ending, but the *fact* the
        # view renders is the logical content).
        return ToolResult(
            output=_MSG_CREATED.format(path=full_path, verb=verb),
            file_changes=[FileChange(path=full_path, old=old, new=content.replace("\r\n", "\n"))],
        )
