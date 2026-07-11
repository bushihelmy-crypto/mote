"""Read file tool — aligned with Claude Code's Read (FileReadTool).

Reads a file from the local filesystem. Text files return contents with
``cat -n`` style line numbers (``offset``/``limit`` select a slice). Images are
returned as a textual placeholder plus the actual bytes as supplemental
multimodal content (ToolResult.images / .pdfs), mirroring CC's approach of
sending media as a separate block rather than inside the tool_result string.
Jupyter notebooks are flattened to readable text.

Rich documents (PDF / Word / Excel) are read two ways, chosen by ``mode``:
- ``"text"`` (default): the document's text is extracted (via the shared
  ``_document`` module) and returned with line numbers. The extraction and line
  model are the SAME ones the Grep tool searches, so a Grep hit reported as
  ``report.pdf:42`` is read with ``offset=42`` — Grep→Read offsets line up.
  Works for PDF, Word and Excel.
- ``"visual"`` (PDF only): the raw bytes are sent to the model as supplemental
  ``pdfs`` content, like CC's PDF reading.

Differences from Claude Code's tool, by design:
- Images are downscaled to fit MAX_IMAGE_DIMENSION (longest edge) before being
  shown to the model, mirroring codex's view_image "high" detail. The original
  format and ICC/EXIF (orientation) metadata are preserved on re-encode. Pass
  ``detail="original"`` to skip the resize and send the raw bytes. This requires
  Pillow; if Pillow is unavailable or the image cannot be decoded, the read
  fails (no silent fallback).
- Notebooks are rendered to text (cells + outputs) rather than structured cells,
  since this framework's tool result is text + media, not arbitrary blocks.
- Dedup state is kept per tool instance (one instance per Role/session) instead
  of a shared readFileState, so it needs no extra Role capability.

The shape (offset is 1-indexed, default 2000-line cap, per-line length cap,
size guard, blocked device paths, empty/short-file reminders) mirrors the CC
tool so model behavior stays familiar.
"""
from __future__ import annotations

import base64
import io
import json
import os
from typing import Callable, ClassVar, Optional

from mote.common.const.tools import (
    DEFAULT_MAX_LINES,
    MAX_FILE_SIZE_BYTES,
    MAX_IMAGE_DIMENSION,
    MAX_LINE_LENGTH,
    MAX_MEDIA_SIZE_BYTES,
)
from mote.common.prompt.tools import FILE_UNCHANGED_STUB, READ_DESCRIPTION
from mote.common.text import count_noun, system_reminder, verb_agree
from mote.executor.base_tool import BaseTool
from mote.executor.dependency._document import document_lines, extract_document_text, is_document
from mote.executor.dependency._paths import resolve_path
from mote.executor.tool_registry import register_tool
from mote.executor.tool_result import ToolError, ToolResult

# Complete model-facing message sentences, hoisted to module-top templates so the
# wording lives in one place (fill via ``.format(...)`` at the raise/return site).
# Structural line-number assembly stays inline in the readers.
_MSG_FILE_PATH_REQUIRED = "Error: 'file_path' argument is required."
_MSG_INVALID_MODE = "Error: invalid mode '{mode}'. Must be 'text' or 'visual'."
_MSG_INVALID_DETAIL = (
    "Error: invalid detail '{detail}'. Must be 'high' (downscale to fit "
    "{max_dim} px) or 'original' (native resolution)."
)
_MSG_BLOCKED_DEVICE = "Error: cannot read '{path}': this device file would block or produce " "infinite output."
_MSG_BINARY_FILE = "Error: this tool cannot read binary files. The file appears to be a " "binary '{ext}' file."
_MSG_FILE_NOT_EXIST = (
    "Error: file does not exist. Note that relative paths resolve against the " "working directory {base}."
)
_MSG_IS_DIRECTORY = (
    "Error: '{path}' is a directory, not a file. Use an ls command via the " "Bash tool to list a directory."
)
_MSG_CANNOT_STAT = "Error: cannot stat '{path}': {error}"
_MSG_CANNOT_READ = "Error: cannot read '{path}': {error}"
_MSG_VISUAL_PDF_ONLY = (
    "Error: mode 'visual' is only supported for PDF files; '{ext}' documents " "can only be read as text (mode='text')."
)
_MSG_FILE_TOO_LARGE = (
    "Error: file ({size} bytes) exceeds the maximum allowed size ({max_size} "
    "bytes). Use the offset and limit parameters to read specific portions of "
    "the file, or search for specific content instead."
)
_MSG_NOT_UTF8 = "Error: this tool cannot read binary files. The file '{path}' is not valid " "UTF-8 text."
_MSG_MEDIA_TOO_LARGE = "Error: {kind} '{path}' ({size} bytes) exceeds the maximum allowed size " "({max_size} bytes)."
_MSG_CANNOT_EXTRACT = "Error: cannot extract text from '{path}': {error}"
_MSG_NO_EXTRACTOR = (
    "Error: cannot read '{path}': no extractor is available for this document "
    "type (install the optional dependency, e.g. pymupdf/pdfminer/pypdf for "
    "PDF, python-docx for Word, openpyxl for Excel)."
)
_MSG_PILLOW_MISSING = (
    "Error: cannot process image '{path}' with detail='high': Pillow is not "
    "installed. Install Pillow or pass detail='original' to send the image at "
    "its native resolution."
)
_MSG_CANNOT_PROCESS_IMAGE = (
    "Error: cannot process image '{path}': {error}. Pass detail='original' to " "send the raw bytes without resizing."
)
_MSG_NOTEBOOK_INVALID_JSON = "Error: '{path}' is not a valid notebook (invalid JSON): {error}"
_MSG_LINE_TRUNCATED_NOTE = "Note: {count} exceeded {max_len} characters and {verb} truncated."
_MSG_EMPTY_FILE = "Warning: the file exists but the contents are empty."
_MSG_SHORTER_THAN_OFFSET = (
    "Warning: the file exists but is shorter than the provided offset " "({offset}). The file has {total} lines."
)
_MSG_DOCUMENT_EMPTY = "Warning: the document exists but no text could be extracted."
_MSG_DOCUMENT_SHORTER_THAN_OFFSET = (
    "Warning: the document exists but is shorter than the provided offset "
    "({offset}). The document has {total} lines."
)
_MSG_NOTEBOOK_NO_CELLS = "Warning: the notebook exists but has no cells."
_MSG_IMAGE_OUTPUT = "Read image {path} ({ext}, {size} bytes; {note}). Shown below."
_MSG_PDF_OUTPUT = "Read PDF {path} ({size} bytes). Shown below."

# Image extensions rendered as multimodal image content.
_IMAGE_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "gif", "webp"})

# Binary extensions this tool still refuses (no text/image/pdf/notebook path).
# .docx/.xlsx are intentionally NOT here: they are rich documents read via text
# extraction (see _document) so Grep's "path:line" maps to Read's offset. Legacy
# binary Office formats (.doc/.xls/.ppt/.pptx) have no extractor and stay blocked.
_BINARY_EXTENSIONS = frozenset(
    {
        "bmp",
        "ico",
        "tiff",
        "svg",
        "zip",
        "gz",
        "tar",
        "tgz",
        "bz2",
        "xz",
        "7z",
        "rar",
        "exe",
        "dll",
        "so",
        "dylib",
        "bin",
        "o",
        "a",
        "class",
        "pyc",
        "pyd",
        "mp3",
        "mp4",
        "wav",
        "avi",
        "mov",
        "mkv",
        "flac",
        "ogg",
        "woff",
        "woff2",
        "ttf",
        "eot",
        "otf",
        "doc",
        "xls",
        "ppt",
        "pptx",
    }
)

# Device files that would hang the process (infinite output / blocking input).
# Path-only check, no I/O. Safe devices like /dev/null are intentionally absent.
_BLOCKED_DEVICE_PATHS = frozenset(
    {
        "/dev/zero",
        "/dev/random",
        "/dev/urandom",
        "/dev/full",
        "/dev/stdin",
        "/dev/tty",
        "/dev/console",
        "/dev/stdout",
        "/dev/stderr",
        "/dev/fd/0",
        "/dev/fd/1",
        "/dev/fd/2",
    }
)


def _is_blocked_device(path: str) -> bool:
    if path in _BLOCKED_DEVICE_PATHS:
        return True
    if path.startswith("/proc/") and path.endswith(("/fd/0", "/fd/1", "/fd/2")):
        return True
    return False


def _add_line_numbers(lines: list[str], start_line: int) -> str:
    """Format lines with right-aligned line numbers, CC `cat -n` arrow style."""
    out = []
    for i, line in enumerate(lines):
        num = str(i + start_line)
        prefix = num if len(num) >= 6 else num.rjust(6)
        out.append(f"{prefix}→{line}")
    return "\n".join(out)


def _cell_source(cell: dict) -> str:
    """Join a notebook cell's source (list[str] or str) into one string."""
    src = cell.get("source", "")
    if isinstance(src, list):
        return "".join(src)
    return src or ""


def _render_outputs(cell: dict) -> list[str]:
    """Render a code cell's text outputs (stdout/stream, text/plain, errors)."""
    rendered: list[str] = []
    for out in cell.get("outputs", []) or []:
        otype = out.get("output_type")
        if otype == "stream":
            text = out.get("text", "")
            rendered.append("".join(text) if isinstance(text, list) else text)
        elif otype in ("execute_result", "display_data"):
            data = out.get("data", {}) or {}
            text = data.get("text/plain", "")
            rendered.append("".join(text) if isinstance(text, list) else text)
            if "image/png" in data or "image/jpeg" in data:
                rendered.append("[image output omitted]")
        elif otype == "error":
            tb = out.get("traceback", []) or []
            rendered.append("\n".join(tb) if isinstance(tb, list) else str(tb))
    return [r for r in rendered if r]


def _render_notebook(nb: dict) -> str:
    """Flatten a parsed notebook into readable text (cells + text outputs)."""
    parts: list[str] = []
    for i, cell in enumerate(nb.get("cells", []) or [], start=1):
        ctype = cell.get("cell_type", "code")
        source = _cell_source(cell)
        header = f"# ── Cell {i} [{ctype}] ──"
        parts.append(header)
        if source:
            parts.append(source.rstrip("\n"))
        if ctype == "code":
            outputs = _render_outputs(cell)
            if outputs:
                parts.append("# Output:")
                parts.append("\n".join(o.rstrip("\n") for o in outputs))
    return "\n".join(parts)


@register_tool
class Read(BaseTool):
    """Read a file from the local filesystem (text, image, PDF, or notebook)."""

    name = "Read"
    aliases: ClassVar[list[str]] = ["Read.run", "read"]
    # Read-only observation: the file can always be re-read, so a cleared result
    # body is recoverable on demand.
    reconstructable: ClassVar[bool] = True
    # Read can return large files; allow a higher cap before persisting (CC).
    max_result_size_chars: ClassVar[int] = 100_000
    description = READ_DESCRIPTION
    # Records each successful read into the Role's shared file-read state so the
    # Write/Edit tools can enforce read-before-overwrite; get_cwd is the stable
    # base for resolving relative paths. Optional: when the tool is used unbound
    # (no Role), these stay unset — recording is skipped and get_cwd falls back
    # to the process cwd.
    requires = ("record_file_read", "get_cwd")

    # Injected from Role by bind(): Role.record_file_read, Role.get_cwd.
    record_file_read: Callable[[str, int], None]
    get_cwd: Callable[[], str]

    def __init__(self) -> None:
        super().__init__()
        # Dedup cache: full_path -> (offset, limit, mtime_ns). One instance per
        # Role, so this is naturally session-scoped. Only text reads are cached.
        self._read_state: dict[str, tuple[int, int | None, int]] = {}

    def _mark_read(self, full_path: str, mtime_ns: int) -> None:
        """Record a successful read into the Role's shared file-read state.

        No-op when the tool is unbound (record_file_read not injected), so the
        Read tool keeps working in isolation/tests.
        """
        recorder = getattr(self, "record_file_read", None)
        if recorder is not None:
            recorder(full_path, mtime_ns)

    async def call(
        self,
        *,
        file_path: str,
        offset: int = 1,
        limit: Optional[int] = None,
        mode: str = "text",
        detail: str = "high",
    ):
        """Read a file from the local filesystem.

        Supports text files (returned with line numbers), images (png/jpg/jpeg/
        gif/webp), rich documents — PDF (.pdf), Word (.docx), Excel (.xlsx) —
        and Jupyter notebooks (.ipynb).

        Rich documents are read two ways, selected by ``mode``:
        - ``"text"`` (default): extract the document's text and return it with
          line numbers, honoring offset/limit. Line numbers match what the Grep
          tool reports, so a Grep hit at ``report.pdf:42`` is read with
          ``offset=42``. Works for PDF, Word and Excel.
        - ``"visual"``: send the raw bytes to the model to view (base64). Only
          PDFs (and images) support this; offset/limit are ignored.

        Args:
            file_path: Absolute path to the file to read (~ is expanded;
                relative paths resolve against the current working directory).
            offset: 1-indexed line number to start reading from. Only needed for
                large text files / documents (default 1, the start of the file).
            limit: Maximum number of lines to read. Only needed for large text
                files / documents (default reads up to 2000 lines).
            mode: For rich documents (PDF/Word/Excel), how to read them: "text"
                (default; extract text with line numbers, aligned to Grep's
                offsets) or "visual" (render the document to the model as bytes;
                PDF only). Ignored for plain text and notebooks.
            detail: For images, the level of detail to send to the model: "high"
                (default; downscale so the longest edge fits within 2048 px to
                save tokens, preserving aspect ratio and format) or "original"
                (send the image at its native resolution). Ignored for non-image
                files.
        """
        if not file_path or not file_path.strip():
            raise ToolError(_MSG_FILE_PATH_REQUIRED)

        if mode not in ("text", "visual"):
            raise ToolError(_MSG_INVALID_MODE.format(mode=mode))

        if detail not in ("high", "original"):
            raise ToolError(_MSG_INVALID_DETAIL.format(detail=detail, max_dim=MAX_IMAGE_DIMENSION))

        full_path = resolve_path(getattr(self, "get_cwd", None), file_path.strip())

        if _is_blocked_device(full_path):
            raise ToolError(_MSG_BLOCKED_DEVICE.format(path=file_path))

        ext = os.path.splitext(full_path)[1].lower().lstrip(".")
        if ext in _BINARY_EXTENSIONS:
            raise ToolError(_MSG_BINARY_FILE.format(ext=ext))

        if not os.path.exists(full_path):
            getter = getattr(self, "get_cwd", None)
            base = (getter() if getter is not None else None) or os.getcwd()
            raise ToolError(_MSG_FILE_NOT_EXIST.format(base=base))

        if os.path.isdir(full_path):
            raise ToolError(_MSG_IS_DIRECTORY.format(path=file_path))

        try:
            stat = os.stat(full_path)
        except OSError as e:
            raise ToolError(_MSG_CANNOT_STAT.format(path=file_path, error=e))

        # Each branch below raises ToolError on failure and otherwise returns a
        # successful result; record the read only once control reaches past the
        # (raising) call, so a failed read never marks the file as seen.
        # --- Image: return bytes as supplemental media ---
        if ext in _IMAGE_EXTENSIONS:
            result = self._read_image(file_path, full_path, ext, stat.st_size, detail)
            self._mark_read(full_path, stat.st_mtime_ns)
            return result
        # --- Rich documents (PDF/Word/Excel) ---
        # "visual" mode renders bytes to the model (PDF only); "text" mode (the
        # default) extracts text with line numbers that match what Grep reports
        # (Grep "path:42" -> offset=42), and also enables Word/Excel reading.
        if is_document(full_path):
            if mode == "visual":
                if ext != "pdf":
                    raise ToolError(_MSG_VISUAL_PDF_ONLY.format(ext=ext))
                result = self._read_pdf(file_path, full_path, stat.st_size)
                self._mark_read(full_path, stat.st_mtime_ns)
                return result
            out = self._read_document(file_path, full_path, offset, limit, stat)
            self._mark_read(full_path, stat.st_mtime_ns)
            return out
        # --- Jupyter notebook: flatten to text ---
        if ext == "ipynb":
            out = self._read_notebook(file_path, full_path)
            self._mark_read(full_path, stat.st_mtime_ns)
            return out

        # --- Text ---
        return self._read_text(file_path, full_path, offset, limit, stat)

    def _read_text(self, file_path, full_path, offset, limit, stat) -> str:
        """Read a text file slice and format with line numbers."""
        # Size guard only applies to whole-file reads (no explicit limit).
        if limit is None and stat.st_size > MAX_FILE_SIZE_BYTES:
            raise ToolError(_MSG_FILE_TOO_LARGE.format(size=stat.st_size, max_size=MAX_FILE_SIZE_BYTES))

        # Normalize offset: callers may pass 0 or 1 to mean "from the start".
        start_line = offset if offset and offset > 0 else 1

        # Dedup: same range + unchanged mtime => point the model at the prior read.
        cached = self._read_state.get(full_path)
        if cached is not None and cached == (start_line, limit, stat.st_mtime_ns):
            # Still a successful "view" of the current content — keep the shared
            # file-read state fresh so a later Write isn't wrongly blocked.
            self._mark_read(full_path, stat.st_mtime_ns)
            return FILE_UNCHANGED_STUB

        try:
            selected, total_lines, truncated_lines = self._read_range(full_path, start_line, limit)
        except UnicodeDecodeError:
            raise ToolError(_MSG_NOT_UTF8.format(path=file_path))
        except OSError as e:
            raise ToolError(_MSG_CANNOT_READ.format(path=file_path, error=e))

        self._read_state[full_path] = (start_line, limit, stat.st_mtime_ns)
        self._mark_read(full_path, stat.st_mtime_ns)

        if total_lines == 0:
            return system_reminder(_MSG_EMPTY_FILE)

        if not selected:
            return system_reminder(_MSG_SHORTER_THAN_OFFSET.format(offset=start_line, total=total_lines))

        body = _add_line_numbers(selected, start_line)
        if truncated_lines:
            verb = verb_agree(truncated_lines, "was", "were")
            body += "\n\n" + system_reminder(
                _MSG_LINE_TRUNCATED_NOTE.format(
                    count=count_noun(truncated_lines, "line"),
                    max_len=MAX_LINE_LENGTH,
                    verb=verb,
                )
            )
        return body

    def _read_document(self, file_path, full_path, offset, limit, stat) -> str:
        """Read a rich document (PDF/Word/Excel) as extracted text, with offset.

        Uses the shared _document extractor + line model so the line numbering
        is identical to what Grep searches: a position Grep reports as
        ``report.pdf:42`` is exactly the line returned here for ``offset=42``.
        Output is formatted with ``cat -n`` line numbers like a text read.
        """
        if stat.st_size > MAX_MEDIA_SIZE_BYTES:
            raise ToolError(
                _MSG_MEDIA_TOO_LARGE.format(
                    kind="document",
                    path=file_path,
                    size=stat.st_size,
                    max_size=MAX_MEDIA_SIZE_BYTES,
                )
            )

        # Normalize offset: callers may pass 0 or 1 to mean "from the start".
        start_line = offset if offset and offset > 0 else 1

        # Dedup: same range + unchanged mtime => point the model at the prior read.
        cached = self._read_state.get(full_path)
        if cached is not None and cached == (start_line, limit, stat.st_mtime_ns):
            self._mark_read(full_path, stat.st_mtime_ns)
            return FILE_UNCHANGED_STUB

        try:
            text = extract_document_text(full_path)
        except Exception as e:  # noqa: BLE001 — surface extraction failure
            raise ToolError(_MSG_CANNOT_EXTRACT.format(path=file_path, error=e))
        if text is None:
            raise ToolError(_MSG_NO_EXTRACTOR.format(path=file_path))

        all_lines = document_lines(text)
        total_lines = len(all_lines)

        self._read_state[full_path] = (start_line, limit, stat.st_mtime_ns)
        self._mark_read(full_path, stat.st_mtime_ns)

        if total_lines == 0 or (total_lines == 1 and all_lines[0] == ""):
            return system_reminder(_MSG_DOCUMENT_EMPTY)

        if start_line > total_lines:
            return system_reminder(_MSG_DOCUMENT_SHORTER_THAN_OFFSET.format(offset=start_line, total=total_lines))

        end_line = start_line + (limit if limit is not None else DEFAULT_MAX_LINES)
        selected = all_lines[start_line - 1 : end_line - 1]

        truncated_lines = 0
        capped: list[str] = []
        for line in selected:
            if len(line) > MAX_LINE_LENGTH:
                line = line[:MAX_LINE_LENGTH] + "... [line truncated]"
                truncated_lines += 1
            capped.append(line)

        body = _add_line_numbers(capped, start_line)
        if truncated_lines:
            verb = verb_agree(truncated_lines, "was", "were")
            body += "\n\n" + system_reminder(
                _MSG_LINE_TRUNCATED_NOTE.format(
                    count=count_noun(truncated_lines, "line"),
                    max_len=MAX_LINE_LENGTH,
                    verb=verb,
                )
            )
        return body

    def _read_image(self, file_path, full_path, ext, size, detail) -> ToolResult:
        """Read an image and return it as supplemental multimodal content.

        With ``detail="high"`` (default) the image is downscaled so its longest
        edge fits within MAX_IMAGE_DIMENSION (mirrors codex view_image), keeping
        aspect ratio, source format and ICC/EXIF metadata; images already within
        the limit are sent unchanged. ``detail="original"`` sends the raw bytes.
        """
        if size > MAX_MEDIA_SIZE_BYTES:
            raise ToolError(
                _MSG_MEDIA_TOO_LARGE.format(
                    kind="image",
                    path=file_path,
                    size=size,
                    max_size=MAX_MEDIA_SIZE_BYTES,
                )
            )
        try:
            with open(full_path, "rb") as f:
                raw = f.read()
        except OSError as e:
            raise ToolError(_MSG_CANNOT_READ.format(path=file_path, error=e))

        final_bytes, note = self._prepare_image_bytes(file_path, raw, detail)
        b64 = base64.b64encode(final_bytes).decode("ascii")
        return ToolResult(
            output=_MSG_IMAGE_OUTPUT.format(path=file_path, ext=ext, size=size, note=note),
            images=[b64],
            data={
                "type": "image",
                "path": full_path,
                "size": size,
                "detail": detail,
                "sent_bytes": len(final_bytes),
            },
        )

    def _prepare_image_bytes(self, file_path: str, raw: bytes, detail: str) -> tuple[bytes, str]:
        """Return (bytes_to_send, human_note), downscaling when detail='high'.

        No silent fallback: when ``detail='high'`` requires Pillow and it is
        missing, or the image cannot be decoded, this raises ToolError instead
        of sending the raw bytes.
        """
        if detail == "original":
            return raw, "original"

        try:
            from PIL import Image
        except ImportError:
            raise ToolError(_MSG_PILLOW_MISSING.format(path=file_path))

        try:
            with Image.open(io.BytesIO(raw)) as im:
                im.load()
                width, height = im.size
                if max(width, height) <= MAX_IMAGE_DIMENSION:
                    return raw, "unchanged"

                fmt = im.format or "PNG"
                # Preserve color/orientation metadata across the re-encode.
                save_kwargs = {}
                exif = im.info.get("exif")
                if exif:
                    save_kwargs["exif"] = exif
                icc = im.info.get("icc_profile")
                if icc:
                    save_kwargs["icc_profile"] = icc

                im.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION), Image.Resampling.BILINEAR)
                buf = io.BytesIO()
                im.save(buf, format=fmt, **save_kwargs)
                new_w, new_h = im.size
                return (
                    buf.getvalue(),
                    f"resized {width}x{height} -> {new_w}x{new_h}",
                )
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001 — surface any decode/encode failure
            raise ToolError(_MSG_CANNOT_PROCESS_IMAGE.format(path=file_path, error=e))

    def _read_pdf(self, file_path, full_path, size) -> ToolResult:
        """Read a PDF and return it as a supplemental document."""
        if size > MAX_MEDIA_SIZE_BYTES:
            raise ToolError(
                _MSG_MEDIA_TOO_LARGE.format(
                    kind="PDF",
                    path=file_path,
                    size=size,
                    max_size=MAX_MEDIA_SIZE_BYTES,
                )
            )
        try:
            with open(full_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
        except OSError as e:
            raise ToolError(_MSG_CANNOT_READ.format(path=file_path, error=e))
        return ToolResult(
            output=_MSG_PDF_OUTPUT.format(path=file_path, size=size),
            pdfs=[b64],
            data={"type": "pdf", "path": full_path, "size": size},
        )

    def _read_notebook(self, file_path, full_path) -> str:
        """Render a Jupyter notebook (.ipynb) as readable text."""
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                nb = json.load(f)
        except (OSError, UnicodeDecodeError) as e:
            raise ToolError(_MSG_CANNOT_READ.format(path=file_path, error=e))
        except json.JSONDecodeError as e:
            raise ToolError(_MSG_NOTEBOOK_INVALID_JSON.format(path=file_path, error=e))

        return _render_notebook(nb) or system_reminder(_MSG_NOTEBOOK_NO_CELLS)

    def _read_range(self, full_path: str, start_line: int, limit: int | None) -> tuple[list[str], int, int]:
        """Return (selected_lines, total_line_count, truncated_line_count).

        Iterates the file line by line so only selected lines are retained in
        memory; lines outside the range are counted but discarded (mirrors CC's
        streaming reader).
        """
        end_line = start_line + (limit if limit is not None else DEFAULT_MAX_LINES)
        selected: list[str] = []
        total = 0
        truncated = 0

        with open(full_path, "r", encoding="utf-8", newline="") as f:
            for idx, raw in enumerate(f, start=1):
                total = idx
                if idx < start_line or idx >= end_line:
                    continue
                # Strip BOM on the first line, normalize line endings.
                line = raw
                if idx == 1 and line.startswith("\ufeff"):
                    line = line[1:]
                line = line.rstrip("\n").rstrip("\r")
                if len(line) > MAX_LINE_LENGTH:
                    line = line[:MAX_LINE_LENGTH] + "... [line truncated]"
                    truncated += 1
                selected.append(line)

        return selected, total, truncated

    def cleanup_session(self, session_id: str) -> None:
        """Drop the per-session dedup cache."""
        self._read_state.clear()
