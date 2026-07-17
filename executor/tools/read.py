"""Read file tool.

Reads a file from the local filesystem. Text files return contents with
``cat -n`` style line numbers (``offset``/``limit`` select a slice). Images are
returned as a textual placeholder plus the actual bytes as supplemental
multimodal content (ToolResult.images / .pdfs), sending media as a separate
block rather than inside the tool_result string.
Jupyter notebooks are flattened to readable text.

Rich documents (PDF / Word / Excel) are read two ways, chosen by ``mode``:
- ``"text"`` (default): the document's text is extracted (via the shared
  ``_document`` module) and returned with line numbers. The extraction and line
  model are the SAME ones the Grep tool searches, so a Grep hit reported as
  ``report.pdf:42`` is read with ``offset=42`` — Grep→Read offsets line up.
  Works for PDF, Word and Excel.
- ``"visual"`` (PDF only): the raw bytes are sent to the model as supplemental
  ``pdfs`` content.

Differences by design:
- Images are downscaled to fit MAX_IMAGE_DIMENSION (longest edge) before being
  shown to the model, mirroring codex's view_image "high" detail. The original
  format and ICC/EXIF (orientation) metadata are preserved on re-encode. Pass
  ``detail="original"`` to skip the resize and send the raw bytes. This requires
  Pillow; if Pillow is unavailable or the image cannot be decoded, the read
  fails (no silent fallback).
- Notebooks are rendered to text (cells + outputs) rather than structured cells,
  since this framework's tool result is text + media, not arbitrary blocks.
- Dedup state is kept per tool instance (one instance per Role/session) instead
  of a shared file-read state. The short-circuit is gated on ContextVisibility
  (via the ``is_resource_visible`` capability): a file whose earlier read has
  been folded/erased from context is re-read in full rather than pointed back at
  a cleared body, honouring the ``reconstructable`` promise that a read result is
  always recoverable on demand.

The shape: offset is 1-indexed; when limit is unset the whole file is read
(a large result is persisted to disk by the shared tool-result exit rather
than truncated here). Size guard, blocked device paths, empty/short-file
reminders round it out.
"""
from __future__ import annotations

import base64
import io
import json
import os
import tempfile
from typing import ClassVar, Optional

from mote.common.const.llm import supports_pdf_input, supports_vision
from mote.common.const.tools import MAX_FILE_SIZE_BYTES, MAX_IMAGE_DIMENSION, MAX_MEDIA_SIZE_BYTES
from mote.common.exception import ToolNotConfiguredError
from mote.common.prompt.tools import FILE_UNCHANGED_STUB
from mote.common.schema import ToolEffect
from mote.common.text import system_reminder
from mote.executor.base_tool import BaseTool
from mote.executor.capability_types import GetCwd, GetDefaultModel, IsResourceVisible, RecordFileRead
from mote.executor.dependency._document import document_lines, extract_document_text, is_document
from mote.executor.dependency._paths import resolve_path
from mote.executor.dependency._video import VIDEO_EXTENSIONS, VideoError, VideoUnavailable, decompose_video
from mote.executor.tool_registry import register_tool
from mote.executor.tool_result import ToolError, ToolMedia, ToolResult

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
_MSG_VIDEO_UNAVAILABLE = (
    "Video understanding is unavailable: {error}. Install ffmpeg + ffprobe (the "
    "video decode kernel) so Read can decompose a video into frames + transcript."
)
_MSG_IMAGE_MODEL_UNSUPPORTED = (
    "Cannot read image '{path}': the default model '{model}' is not vision-capable, "
    "so an attached image would never reach it. Configure a multimodal (vision) "
    "model as models.default to read images."
)
_MSG_PDF_MODEL_UNSUPPORTED = (
    "Cannot read PDF '{path}': the default model '{model}' does not accept native "
    "PDF (document) input, so an attached PDF would never reach it. Configure a "
    "PDF-capable model (e.g. a Claude model) as models.default, or extract the "
    "PDF's text another way."
)
_MSG_VIDEO_FAILED = "Could not read video '{path}': {error}"
_MSG_VIDEO_NO_FRAMES = (
    "No frames could be extracted from '{path}'. The file may be corrupt, " "not a video, or an unsupported codec."
)
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

# Frame budget for a video read — a hard ceiling so a single read cannot flood
# the context with hundreds of images. The kernel spreads frames across the clip
# (first + last always kept) and drops near-duplicates before this cap.
_VIDEO_MAX_FRAMES = 60

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
    """Format lines with right-aligned line numbers, `cat -n` arrow style."""
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


def _clock(seconds: float) -> str:
    """A ``MM:SS`` / ``H:MM:SS`` label for a frame timestamp."""
    total = int(round(seconds))
    minutes, sec = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"


def _video_summary(file_path: str, result, *, kept: int) -> str:
    """Assemble the text half of a video read: header, frame index, transcript."""
    meta = result.meta
    title = meta.get("title") or file_path
    lines = [f"Read video: {title}"]
    duration = meta.get("duration_seconds") or meta.get("duration")
    if duration:
        lines.append(f"Duration: {int(float(duration))}s")
    if meta.get("width") and meta.get("height"):
        lines.append(f"Resolution: {meta['width']}x{meta['height']}")
    lines.append(f"Extracted {kept} frame(s) via the {result.engine} engine; shown below in order.")
    for note in result.notes:
        lines.append(f"Note: {note}")
    # A timestamp index so the model can map each shown frame to its moment.
    if result.frames[:kept]:
        stamps = ", ".join(_clock(f.timestamp) for f in result.frames[:kept])
        lines.append(f"Frame timestamps: {stamps}")
    if result.transcript:
        lines.append("")
        lines.append("Transcript:")
        lines.append(result.transcript)
    return "\n".join(lines)


@register_tool
class Read(BaseTool):
    """Read a file from the local filesystem (text, image, PDF, or notebook)."""

    name = "Read"
    aliases: ClassVar[list[str]] = ["Read.run", "read"]
    # Read-only observation: the file can always be re-read, so a cleared result
    # body is recoverable on demand.
    reconstructable: ClassVar[bool] = True
    # No side effect — opt out of the effect ledger (safe to replay always).
    effect: ClassVar[ToolEffect] = ToolEffect.PURE
    # Read can return large files; allow a higher cap before persisting.
    max_result_size_chars: ClassVar[int] = 100_000
    # Records each successful read into the Role's shared file-read state so the
    # Write/Edit tools can enforce read-before-overwrite; get_cwd is the stable
    # base for resolving relative paths; is_resource_visible lets the dedup
    # short-circuit check whether a prior read of this file is still present in
    # context before pointing the model back at it. Optional: when the tool is
    # used unbound (no Role), these stay unset — recording is skipped, get_cwd
    # falls back to the process cwd, and dedup assumes the prior read is visible.
    # get_default_model lets the image/PDF readers refuse up-front when the main
    # model cannot read that media (rather than attach media it silently drops).
    requires = ("record_file_read", "get_cwd", "is_resource_visible", "get_default_model")

    # Injected from Role by bind(): Role.record_file_read, Role.get_cwd,
    # Role.is_resource_visible, Role.get_default_model.
    record_file_read: RecordFileRead
    get_cwd: GetCwd
    is_resource_visible: IsResourceVisible
    get_default_model: GetDefaultModel

    def __init__(self) -> None:
        super().__init__()
        # Dedup cache: full_path -> (offset, limit, mtime_ns). One instance per
        # Role, so this is naturally session-scoped. Only text reads are cached.
        self._read_state: dict[str, tuple[int, int | None, int]] = {}

    def _default_model(self) -> Optional[str]:
        """The main model's name, or None when unbound / unconfigured.

        No-op-safe when the tool is used standalone (no Role): returns None, so
        the media capability guards below skip and the read proceeds unchanged.
        """
        getter = getattr(self, "get_default_model", None)
        return getter() if getter is not None else None

    def _mark_read(self, full_path: str, mtime_ns: int) -> None:
        """Record a successful read into the Role's shared file-read state.

        No-op when the tool is unbound (record_file_read not injected), so the
        Read tool keeps working in isolation/tests.
        """
        recorder = getattr(self, "record_file_read", None)
        if recorder is not None:
            recorder(full_path, mtime_ns)

    def _prior_read_visible(self, full_path: str) -> bool:
        """Is this file's earlier read result still present in the model's context?

        Consults the injected ``is_resource_visible`` capability (the
        ContextVisibility authority). Returns True when unbound (no capability),
        so a standalone Read keeps its simple dedup behaviour; the visibility
        gate only tightens dedup when a real Role/context is present. A False
        answer means compaction folded or erased the earlier result, so the
        caller must return real content instead of the "unchanged" stub.
        """
        checker = getattr(self, "is_resource_visible", None)
        if checker is None:
            return True
        return bool(checker(full_path))

    @staticmethod
    def _tagged(output: str, full_path: str) -> ToolResult:
        """Wrap real read content as a ToolResult tagged with its source file.

        The ``resource_path`` rides the result → the tool_result message's
        metadata (``TOOL_RESULT_RESOURCE_PATH``), where ContextVisibility keys
        off it to decide whether this file's latest read is still present. Only
        real content is tagged; the dedup stub deliberately is not, so it never
        masks (as "still present") a prior read that has since been folded.
        """
        return ToolResult(output=output, resource_path=full_path)

    async def call(
        self,
        *,
        file_path: str,
        offset: int = 1,
        limit: Optional[int] = None,
        mode: str = "text",
        detail: str = "high",
    ):
        """Read a local file's contents — text, images, PDFs, notebooks — with line numbers.

        Reads a file from the local filesystem. The file_path may be absolute, or
        relative to the working directory; ~ is expanded.

        - By default it reads the whole file from the start. Use offset
          (1-indexed start line) and limit to read a specific slice of a large
          file; a Grep hit reported as path:42 is read with offset=42.
        - Output is returned with cat -n style line numbers (a right-aligned
          number then an arrow then the line). These numbers are for your
          reference only — never reproduce the number+arrow prefix when quoting
          or editing content.
        - Images (png/jpg/jpeg/gif/webp) and PDFs (mode='visual') are shown to
          you visually; Jupyter notebooks (.ipynb) are rendered as text; rich
          documents (PDF/Word/Excel) are extracted to text with line numbers by
          default.
        - You may read multiple distinct files in a single turn by making several
          Read calls at once; prefer this over reading them one at a time.
        - ALWAYS use this tool to read files instead of shell commands like cat /
          head / tail: it handles line numbering, large-file slicing, and media.
          If a file was read and is unchanged, a short 'unchanged' note may be
          returned in place of the body — that is expected.

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
            limit: Maximum number of lines to read. Omit to read to the end of
                the file; set it only to read a specific slice of a large file.
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
        # A local video is decoded into timestamped frames (shown as images) plus
        # a transcript — Read's video branch, the same way it absorbs an image or
        # a PDF. Checked before the binary rejection below, since video extensions
        # live in _BINARY_EXTENSIONS. (Networked video is out of scope: fetch a
        # URL to a local file first, e.g. bash `yt-dlp -o clip.mp4 <url>`, then
        # Read that local file.)
        if ext in VIDEO_EXTENSIONS:
            if not os.path.exists(full_path):
                getter = getattr(self, "get_cwd", None)
                base = (getter() if getter is not None else None) or os.getcwd()
                raise ToolError(_MSG_FILE_NOT_EXIST.format(base=base))
            if os.path.isdir(full_path):
                raise ToolError(_MSG_IS_DIRECTORY.format(path=file_path))
            result = await self._read_video(file_path, full_path)
            try:
                self._mark_read(full_path, os.stat(full_path).st_mtime_ns)
            except OSError:
                pass
            if result.success:
                result.resource_path = full_path
            return result
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
            result.resource_path = full_path
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
                result.resource_path = full_path
                return result
            return self._read_document(file_path, full_path, offset, limit, stat)
        # --- Jupyter notebook: flatten to text ---
        if ext == "ipynb":
            out = self._read_notebook(file_path, full_path)
            self._mark_read(full_path, stat.st_mtime_ns)
            return self._tagged(out, full_path)

        # --- Text ---
        return self._read_text(file_path, full_path, offset, limit, stat)

    def _read_text(self, file_path, full_path, offset, limit, stat) -> "str | ToolResult":
        """Read a text file slice and format with line numbers.

        Real content is returned as a ``ToolResult`` tagged with ``resource_path``
        (so ContextVisibility can later tell whether this read is still present);
        the dedup short-circuit returns the bare ``FILE_UNCHANGED_STUB`` string,
        deliberately untagged so it never registers as this file's latest result.
        """
        # Size guard only applies to whole-file reads (no explicit limit).
        if limit is None and stat.st_size > MAX_FILE_SIZE_BYTES:
            raise ToolError(_MSG_FILE_TOO_LARGE.format(size=stat.st_size, max_size=MAX_FILE_SIZE_BYTES))

        # Normalize offset: callers may pass 0 or 1 to mean "from the start".
        start_line = offset if offset and offset > 0 else 1

        # Dedup: same range + unchanged mtime => the model already has this exact
        # view — but ONLY short-circuit if that earlier result is still present
        # in context. Compaction may have folded/erased it; pointing the model
        # back at a cleared body would strand it with no content, so in that case
        # fall through and return the real bytes (honouring reconstructable=True:
        # a cleared read is recoverable on demand). Unbound (no visibility
        # capability) assumes the prior read is still visible, preserving the
        # standalone dedup behaviour.
        cached = self._read_state.get(full_path)
        if (
            cached is not None
            and cached == (start_line, limit, stat.st_mtime_ns)
            and self._prior_read_visible(full_path)
        ):
            # Still a successful "view" of the current content — keep the shared
            # file-read state fresh so a later Write isn't wrongly blocked.
            self._mark_read(full_path, stat.st_mtime_ns)
            return FILE_UNCHANGED_STUB

        try:
            selected, total_lines = self._read_range(full_path, start_line, limit)
        except UnicodeDecodeError:
            raise ToolError(_MSG_NOT_UTF8.format(path=file_path))
        except OSError as e:
            raise ToolError(_MSG_CANNOT_READ.format(path=file_path, error=e))

        self._read_state[full_path] = (start_line, limit, stat.st_mtime_ns)
        self._mark_read(full_path, stat.st_mtime_ns)

        if total_lines == 0:
            return self._tagged(system_reminder(_MSG_EMPTY_FILE), full_path)

        if not selected:
            return self._tagged(
                system_reminder(_MSG_SHORTER_THAN_OFFSET.format(offset=start_line, total=total_lines)),
                full_path,
            )

        body = _add_line_numbers(selected, start_line)
        return self._tagged(body, full_path)

    def _read_document(self, file_path, full_path, offset, limit, stat) -> "str | ToolResult":
        """Read a rich document (PDF/Word/Excel) as extracted text, with offset.

        Uses the shared _document extractor + line model so the line numbering
        is identical to what Grep searches: a position Grep reports as
        ``report.pdf:42`` is exactly the line returned here for ``offset=42``.
        Output is formatted with ``cat -n`` line numbers like a text read.

        Real content is returned as a ``ToolResult`` tagged with ``resource_path``
        (mirrors ``_read_text``); the dedup short-circuit returns the bare
        ``FILE_UNCHANGED_STUB`` string, gated on ContextVisibility so a folded
        prior read falls through to fresh content instead of a stranded stub.
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

        # Dedup: same range + unchanged mtime => point the model at the prior
        # read, but ONLY when that earlier result is still present in context
        # (see _read_text for the rationale). A folded/erased prior read falls
        # through to fresh extraction.
        cached = self._read_state.get(full_path)
        if (
            cached is not None
            and cached == (start_line, limit, stat.st_mtime_ns)
            and self._prior_read_visible(full_path)
        ):
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
            return self._tagged(system_reminder(_MSG_DOCUMENT_EMPTY), full_path)

        if start_line > total_lines:
            return self._tagged(
                system_reminder(_MSG_DOCUMENT_SHORTER_THAN_OFFSET.format(offset=start_line, total=total_lines)),
                full_path,
            )

        # No explicit limit reads to end-of-document; a large result is handled
        # by the single persist-to-disk exit, not truncated here.
        selected = all_lines[start_line - 1 :] if limit is None else all_lines[start_line - 1 : start_line - 1 + limit]

        body = _add_line_numbers(selected, start_line)
        return self._tagged(body, full_path)

    def _read_image(self, file_path, full_path, ext, size, detail) -> ToolResult:
        """Read an image and return it as supplemental multimodal content.

        With ``detail="high"`` (default) the image is downscaled so its longest
        edge fits within MAX_IMAGE_DIMENSION (mirrors codex view_image), keeping
        aspect ratio, source format and ICC/EXIF metadata; images already within
        the limit are sent unchanged. ``detail="original"`` sends the raw bytes.

        Refuses up-front with :class:`ToolNotConfiguredError` when the main model
        is not vision-capable — an attached image would never reach it.
        """
        model = self._default_model()
        if model is not None and not supports_vision(model):
            raise ToolNotConfiguredError(_MSG_IMAGE_MODEL_UNSUPPORTED.format(path=file_path, model=model))
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
            media=[ToolMedia(kind="image", b64=b64, ref=full_path)],
            data={
                "type": "image",
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

    async def _read_video(self, file_path, full_path) -> ToolResult:
        """Decode a local video into timestamped frames + a transcript.

        Frames are returned as supplemental image ``ToolMedia`` (a frame IS an
        image — Read's existing vision outlet) plus a text summary carrying the
        metadata and the timestamped transcript. The heavy decode runs in the
        shared ``_video`` kernel (ffmpeg/ffprobe); a missing tool raises
        :class:`ToolNotConfiguredError`, a decode failure a hard error.
        """
        # A per-call scratch dir for the extracted frames; removed on exit (the
        # frame bytes are already carried in the result media).
        with tempfile.TemporaryDirectory(prefix="mote-video-") as work:
            try:
                result = await decompose_video(full_path, work, max_frames=_VIDEO_MAX_FRAMES)
            except VideoUnavailable as e:
                raise ToolNotConfiguredError(_MSG_VIDEO_UNAVAILABLE.format(error=e))
            except VideoError as e:
                raise ToolError(_MSG_VIDEO_FAILED.format(path=file_path, error=e))

            if not result.frames:
                raise ToolError(_MSG_VIDEO_NO_FRAMES.format(path=file_path))

            media = [
                ToolMedia(kind="image", b64=base64.b64encode(frame.jpeg).decode("ascii"), mime="image/jpeg")
                for frame in result.frames
                if len(frame.jpeg) <= MAX_MEDIA_SIZE_BYTES
            ]
            return ToolResult(
                output=_video_summary(file_path, result, kept=len(media)),
                media=media,
                data={
                    "type": "video",
                    "frames": len(media),
                    "engine": result.engine,
                    "has_transcript": bool(result.transcript),
                },
            )

    def _read_pdf(self, file_path, full_path, size) -> ToolResult:
        """Read a PDF and return it as a supplemental document.

        Refuses up-front with :class:`ToolNotConfiguredError` when the main model
        does not accept native PDF input — an attached PDF would never reach it.
        """
        model = self._default_model()
        if model is not None and not supports_pdf_input(model):
            raise ToolNotConfiguredError(_MSG_PDF_MODEL_UNSUPPORTED.format(path=file_path, model=model))
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
            media=[ToolMedia(kind="pdf", b64=b64, ref=full_path, mime="application/pdf")],
            data={"type": "pdf", "size": size},
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

    def _read_range(self, full_path: str, start_line: int, limit: int | None) -> tuple[list[str], int]:
        """Return (selected_lines, total_line_count).

        Iterates the file line by line so only selected lines are retained in
        memory; lines outside the range are counted but discarded (streaming
        reader). When ``limit`` is None the range runs to end-of-file — a large
        result is handled downstream by the single persist-to-disk exit, not by
        truncating here. Individual long lines are returned intact for the same
        reason.
        """
        end_line = start_line + limit if limit is not None else None
        selected: list[str] = []
        total = 0

        with open(full_path, "r", encoding="utf-8", newline="") as f:
            for idx, raw in enumerate(f, start=1):
                total = idx
                if idx < start_line or (end_line is not None and idx >= end_line):
                    continue
                # Strip BOM on the first line, normalize line endings.
                line = raw
                if idx == 1 and line.startswith("\ufeff"):
                    line = line[1:]
                line = line.rstrip("\n").rstrip("\r")
                selected.append(line)

        return selected, total

    def cleanup_session(self, session_id: str) -> None:
        """Drop the per-session dedup cache."""
        self._read_state.clear()
