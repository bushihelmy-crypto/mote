"""Read file tool.

Reads a file from the local filesystem. Text files return contents with
``cat -n`` style line numbers (``offset``/``limit`` select a slice). Images and
PDFs are published as immutable Artifacts and returned as supplemental media
references rather than embedding bytes in the tool result.
Jupyter notebooks are flattened to readable text.

Rich documents (PDF / Word / Excel) are read through managed snapshot views:
- ``"text"`` (default): the document's text is extracted by File Operations
  and returned with line numbers. The extraction and line
  model are the SAME ones the Grep tool searches, so a Grep hit reported as
  ``report.pdf:42`` is read with ``offset=42`` — Grep→Read offsets line up.
  Works for PDF, Word and Excel.
- ``"visual"`` (PDF only): the raw bytes are sent to the model as supplemental
  ``pdfs`` content.
- ``"render"`` (PDF only): selected pages are rendered to PNG for models that
  accept images but not native PDF input.
- ``"raw"`` / ``"hex"``: lossless bounded byte views for every regular file.

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

The shape: text offsets are 1-indexed and default to a bounded 2,000-line page;
partial results carry an explicit continuation offset. Size guards, blocked
device paths, and empty/short-file reminders round it out.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from typing import ClassVar, Optional

from mote.contracts.file import (
    ByteReadRequest,
    ByteViewMode,
    ContinueReadRequest,
    DocumentExtractionError,
    EncodingRejectedError,
    FileBinaryContentError,
    FileByteView,
    FileOperationError,
    FileSnapshot,
    FileTextView,
    PdfReadRequest,
    PdfView,
    PdfViewMode,
    TextReadRequest,
)
from mote.contracts.model.capabilities import supports_pdf_input, supports_vision
from mote.contracts.tool.effects import ToolEffect
from mote.contracts.tool.errors import ToolError, ToolNotConfiguredError
from mote.contracts.tool.result import json_tool_payload
from mote.product.toolsets.builtin._paths import resolve_path, resolve_permission_path
from mote.product.toolsets.builtin.read_adapters.byte import format_byte_view
from mote.product.toolsets.builtin.read_adapters.image import ImageProcessingError, prepare_image
from mote.product.toolsets.builtin.read_adapters.notebook import NotebookDecodeError, NotebookJsonError, parse_notebook
from mote.product.toolsets.builtin.read_adapters.pdf import OversizedPdfPage, format_pdf_text, prepare_pdf_render
from mote.product.toolsets.builtin.read_adapters.text import format_text_view
from mote.product.toolsets.builtin.read_adapters.video import (
    VideoDecodeFailed,
    VideoDecodeUnavailable,
    decompose_video_bytes,
    video_summary,
)
from mote.product.toolsets.constants import MAX_IMAGE_DIMENSION, MAX_MEDIA_SIZE_BYTES
from mote.product.toolsets.prompts import FILE_UNCHANGED_STUB
from mote.runtime.artifacts.media import publish_media_artifact
from mote.runtime.context.markers import system_reminder
from mote.runtime.media.video import VIDEO_EXTENSIONS
from mote.runtime.tools.base_tool import BaseTool
from mote.runtime.tools.capability_types import (
    CaptureFileSnapshot,
    GetArtifactPublisher,
    GetCwd,
    GetDefaultModel,
    IsResourceVisible,
    ObserveFileSnapshot,
    ReadFileView,
)
from mote.runtime.tools.tool_result import ToolMedia, ToolResult

# Complete model-facing message sentences, hoisted to module-top templates so the
# wording lives in one place (fill via ``.format(...)`` at the raise/return site).
# Structural line-number assembly stays inline in the readers.
_MSG_FILE_PATH_REQUIRED = "Error: 'file_path' argument is required."
_MSG_INVALID_MODE = "Error: invalid mode '{mode}'. Must be 'text', 'visual', 'render', 'raw', or 'hex'."
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
    "vision-capable model on the default route to read images."
)
_MSG_PDF_MODEL_UNSUPPORTED = (
    "Cannot read PDF '{path}': the default model '{model}' does not accept native "
    "PDF (document) input, so an attached PDF would never reach it. Configure a "
    "PDF-capable model (e.g. a Claude model) on the default route, use mode='render' "
    "with a vision model, or use mode='text'."
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
_MSG_CANNOT_READ = "Error: cannot read '{path}': {error}"
_MSG_VISUAL_PDF_ONLY = (
    "Error: mode 'visual' is only supported for PDF files; '{ext}' documents " "can only be read as text (mode='text')."
)
_MSG_PDF_PAGES_ONLY = "Error: pages/page rendering are only supported for PDF files in text or " "render mode."
_MSG_CURSOR_CONFLICT = (
    "Error: cursor fixes the prior read mode and snapshot; do not combine it "
    "with mode, pages, offset, or encoding controls."
)
_MSG_NOT_UTF8 = (
    "Error: cannot determine a lossless text encoding for '{path}'. " "Pass encoding or fallback_encoding explicitly."
)
_MSG_MEDIA_TOO_LARGE = "Error: {kind} '{path}' ({size} bytes) exceeds the maximum allowed size " "({max_size} bytes)."
_MSG_CANNOT_EXTRACT = "Error: cannot extract text from '{path}': {error}"
_MSG_CANNOT_PROCESS_IMAGE = (
    "Error: cannot process image '{path}': {error}. Pass detail='original' to " "send the raw bytes without resizing."
)
_MSG_NOTEBOOK_INVALID_JSON = "Error: '{path}' is not a valid notebook (invalid JSON): {error}"
_MSG_NOTEBOOK_NO_CELLS = "Warning: the notebook exists but has no cells."
_MSG_IMAGE_OUTPUT = "Read image {path} ({ext}, {size} bytes; {note}). Shown below."
_MSG_PDF_OUTPUT = "Read PDF {path} ({size} bytes). Shown below."

# Image extensions rendered as multimodal image content.
_IMAGE_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "gif", "webp"})
_RICH_DOCUMENT_EXTENSIONS = frozenset({"pdf", "docx", "xlsx"})

# Frame budget for a video read — a hard ceiling so a single read cannot flood
# the context with hundreds of images. The kernel spreads frames across the clip
# (first + last always kept) and drops near-duplicates before this cap.
_VIDEO_MAX_FRAMES = 60

# Binary extensions this tool still refuses (no text/image/pdf/notebook path).
# .docx/.xlsx are intentionally NOT here: they are rich documents read via text
# extraction from the sealed artifact so Search's structured line maps to Read's offset. Legacy
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
    requires = (
        "capture_file_snapshot",
        "read_file_view",
        "observe_file_snapshot",
        "get_cwd",
        "is_resource_visible",
        "get_default_model",
        "get_artifact_publisher",
    )

    # Injected from Role by bind through the explicit capability allowlist.
    capture_file_snapshot: CaptureFileSnapshot
    read_file_view: ReadFileView
    observe_file_snapshot: ObserveFileSnapshot
    get_cwd: GetCwd
    is_resource_visible: IsResourceVisible
    get_default_model: GetDefaultModel
    get_artifact_publisher: GetArtifactPublisher

    def permission_target(self, args: dict) -> str:
        """The canonical source path matched by Read path rules."""
        return resolve_permission_path(self.get_cwd, args.get("file_path"))

    def __init__(self) -> None:
        super().__init__()
        # Dedup cache: full_path -> (offset, limit, content digest). One instance per
        # Role, so this is naturally session-scoped. Only text reads are cached.
        self._read_state: dict[str, tuple[object, ...]] = {}

    def _default_model(self) -> Optional[str]:
        """The main model's name, or None when unbound / unconfigured.

        No-op-safe when the tool is used standalone (no Role): returns None, so
        the media capability guards below skip and the read proceeds unchanged.
        """
        getter = getattr(self, "get_default_model", None)
        return getter() if getter is not None else None

    def _mark_read(self, snapshot: FileSnapshot) -> None:
        self.observe_file_snapshot(snapshot)

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
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        mode: str = "text",
        detail: str = "high",
        encoding: Optional[str] = None,
        fallback_encoding: Optional[str] = None,
        pages: str = "",
        pdf_dpi: Optional[int] = None,
        cursor: Optional[str] = None,
    ):
        """Read a local file's contents — text, images, PDFs, notebooks — with line numbers.

        file_path may be absolute or relative to the working directory; ~ is
        expanded. Text reads return at most 2,000 lines by default; partial
        results include a cursor for continuation. A
        Search hit at path:42 is read with offset=42.

        - Output carries cat -n style line numbers (number, arrow, line) for
          reference ONLY — never reproduce the number+arrow prefix when quoting
          or editing.
        - Images (png/jpg/jpeg/gif/webp) and PDFs (mode='visual') are shown
          visually; notebooks (.ipynb) render as text; rich documents
          (PDF/Word/Excel) extract to text with line numbers by default.
        - Read several files in one turn by issuing multiple Read calls at once —
          prefer this over one at a time.
        - ALWAYS use this tool instead of cat/head/tail: it handles line
          numbering, slicing, and media. An unchanged re-read may return a short
          'unchanged' note in place of the body — that is expected.

        ``mode="raw"`` and ``mode="hex"`` work for every regular file and use
        byte offsets. Text/document offsets remain 1-indexed lines.
        PDF ``mode="render"`` converts selected pages to PNG, so vision models
        can inspect them even when they do not accept native PDF input.

        Args:
            file_path: Absolute path to the file to read (~ is expanded;
                relative paths resolve against the current working directory).
            offset: Start position. Text/document modes use a 1-indexed line
                number and default to 1; raw/hex use a 0-indexed byte offset and
                default to 0.
            limit: Maximum lines for text/document modes or bytes for raw/hex.
                All modes use a safe automatic page size when omitted or zero.
            mode: "raw" returns base64 bytes and "hex" returns a diagnostic hex
                dump for any regular file. PDF "render" returns selected pages
                as PNG images. For rich documents, "text"
                (default; extract text with line numbers, aligned to Search
                offsets) or "visual" (render the document to the model as bytes;
                PDF only). Ignored for plain text and notebooks.
            pages: PDF page selection such as "3", "3-5", or "1,4-6".
                Text mode preserves page boundaries; render defaults to page 1.
            pdf_dpi: PDF render resolution from 72 to 300 DPI (default 144).
            cursor: Opaque continuation returned by any earlier partial Read.
                It fixes the mode and immutable snapshot; omit all initial-read
                selectors except file_path and an optional page-size limit.
            detail: For images, the level of detail to send to the model: "high"
                (default; downscale so the longest edge fits within 2048 px to
                save tokens) or "original"
                (send the image at its native resolution). Ignored for non-image
                files.
            encoding: Explicit text encoding, for example gbk or shift_jis.
            fallback_encoding: Encoding to try when automatic detection cannot
                decode the file losslessly.
        """
        if not file_path or not file_path.strip():
            raise ToolError(_MSG_FILE_PATH_REQUIRED)

        if mode not in ("text", "visual", "render", "raw", "hex"):
            raise ToolError(_MSG_INVALID_MODE.format(mode=mode))

        if cursor is not None and (
            mode != "text"
            or pages.strip()
            or offset is not None
            or encoding is not None
            or fallback_encoding is not None
            or pdf_dpi is not None
        ):
            raise ToolError(_MSG_CURSOR_CONFLICT)

        if detail not in ("high", "original"):
            raise ToolError(_MSG_INVALID_DETAIL.format(detail=detail, max_dim=MAX_IMAGE_DIMENSION))

        full_path = resolve_path(self.get_cwd, file_path.strip())

        if _is_blocked_device(full_path):
            raise ToolError(_MSG_BLOCKED_DEVICE.format(path=file_path))

        ext = os.path.splitext(full_path)[1].lower().lstrip(".")
        if cursor is None and not os.path.exists(full_path):
            raise ToolError(_MSG_FILE_NOT_EXIST.format(base=self.get_cwd()))

        if cursor is None and os.path.isdir(full_path):
            raise ToolError(_MSG_IS_DIRECTORY.format(path=file_path))

        if cursor is not None:
            try:
                continued = self.read_file_view(
                    full_path,
                    ContinueReadRequest(cursor=cursor, limit=limit),
                )
            except FileOperationError as e:
                raise ToolError(_MSG_CANNOT_READ.format(path=file_path, error=e))
            if isinstance(continued, FileByteView):
                return self._read_byte_view(file_path, full_path, continued)
            if isinstance(continued, PdfView):
                if continued.mode == PdfViewMode.RENDER:
                    model = self._default_model()
                    if model is not None and not supports_vision(model):
                        raise ToolNotConfiguredError(
                            _MSG_IMAGE_MODEL_UNSUPPORTED.format(
                                path=file_path,
                                model=model,
                            )
                        )
                return await self._read_pdf_pages(file_path, full_path, continued)
            return self._read_text_view(full_path, continued)

        if mode in ("raw", "hex"):
            byte_offset = offset if offset is not None else 0
            try:
                view = self.read_file_view(
                    full_path,
                    ByteReadRequest(
                        mode=ByteViewMode(mode),
                        offset=byte_offset,
                        limit=limit,
                    ),
                )
            except FileOperationError as e:
                raise ToolError(_MSG_CANNOT_READ.format(path=file_path, error=e))
            if not isinstance(view, FileByteView):
                raise ToolError(_MSG_CANNOT_READ.format(path=file_path, error="unexpected byte view"))
            return self._read_byte_view(file_path, full_path, view)

        if mode == "render" or pages.strip():
            if ext != "pdf" or mode not in ("text", "render"):
                raise ToolError(_MSG_PDF_PAGES_ONLY)
            if mode == "render":
                model = self._default_model()
                if model is not None and not supports_vision(model):
                    raise ToolNotConfiguredError(
                        _MSG_IMAGE_MODEL_UNSUPPORTED.format(
                            path=file_path,
                            model=model,
                        )
                    )
            try:
                view = self.read_file_view(
                    full_path,
                    PdfReadRequest(
                        mode=(PdfViewMode.RENDER if mode == "render" else PdfViewMode.TEXT),
                        pages=pages,
                        dpi=pdf_dpi or 144,
                        limit=limit,
                    ),
                )
            except FileOperationError as e:
                raise ToolError(_MSG_CANNOT_READ.format(path=file_path, error=e))
            if not isinstance(view, PdfView):
                raise ToolError(_MSG_CANNOT_READ.format(path=file_path, error="unexpected PDF view"))
            return await self._read_pdf_pages(file_path, full_path, view)

        if ext in _BINARY_EXTENSIONS and ext not in VIDEO_EXTENSIONS:
            raise ToolError(_MSG_BINARY_FILE.format(ext=ext))

        if mode == "visual" and ext in _RICH_DOCUMENT_EXTENSIONS and ext != "pdf":
            raise ToolError(_MSG_VISUAL_PDF_ONLY.format(ext=ext))

        needs_raw = (
            ext in VIDEO_EXTENSIONS or ext in _IMAGE_EXTENSIONS or ext == "ipynb" or (ext == "pdf" and mode == "visual")
        )
        if needs_raw:
            try:
                snapshot, raw = self.capture_file_snapshot(full_path)
            except FileOperationError as e:
                raise ToolError(_MSG_CANNOT_READ.format(path=file_path, error=e))
            if ext in VIDEO_EXTENSIONS:
                result = await self._read_video(file_path, raw, ext)
                self._mark_read(snapshot)
                if result.success:
                    result = replace(result, resource_path=full_path)
                return result
            if ext in _IMAGE_EXTENSIONS:
                result = await self._read_image(file_path, full_path, ext, raw, detail)
                self._mark_read(snapshot)
                result = replace(result, resource_path=full_path)
                return result
            if ext == "pdf":
                result = await self._read_pdf(file_path, full_path, raw)
                self._mark_read(snapshot)
                result = replace(result, resource_path=full_path)
                return result
            out = self._read_notebook(file_path, raw)
            self._mark_read(snapshot)
            return self._tagged(out, full_path)

        try:
            view = self.read_file_view(
                full_path,
                TextReadRequest(
                    offset=offset,
                    limit=limit,
                    encoding=encoding,
                    fallback_encoding=fallback_encoding,
                ),
            )
        except EncodingRejectedError:
            raise ToolError(_MSG_NOT_UTF8.format(path=file_path))
        except DocumentExtractionError as e:
            raise ToolError(_MSG_CANNOT_EXTRACT.format(path=file_path, error=e))
        except FileBinaryContentError:
            raise ToolError(_MSG_BINARY_FILE.format(ext=ext or "unknown"))
        except FileOperationError as e:
            raise ToolError(_MSG_CANNOT_READ.format(path=file_path, error=e))
        if not isinstance(view, FileTextView):
            raise ToolError(_MSG_CANNOT_READ.format(path=file_path, error="unexpected text view"))
        return self._read_text_view(full_path, view)

    def _read_byte_view(
        self,
        file_path: str,
        full_path: str,
        view: FileByteView,
    ) -> "str | ToolResult":
        cache_key = (
            view.mode.value,
            view.offset,
            len(view.data),
            view.snapshot.version.digest,
        )
        if self._read_state.get(full_path) == cache_key and self._prior_read_visible(full_path):
            self._mark_read(view.snapshot)
            return FILE_UNCHANGED_STUB

        self._read_state[full_path] = cache_key
        self._mark_read(view.snapshot)
        output, data = format_byte_view(file_path, view)
        return ToolResult(
            output=output,
            resource_path=full_path,
            payload=json_tool_payload(data),
        )

    async def _read_pdf_pages(
        self,
        file_path: str,
        full_path: str,
        view: PdfView,
    ) -> ToolResult:
        self._mark_read(view.snapshot)
        if view.mode == PdfViewMode.TEXT:
            output, data = format_pdf_text(view)
            return ToolResult(
                output=output,
                resource_path=full_path,
                payload=json_tool_payload(data),
            )

        try:
            publishable_pages, descriptions, data = prepare_pdf_render(
                view,
                max_page_bytes=MAX_MEDIA_SIZE_BYTES,
            )
        except OversizedPdfPage as exc:
            raise ToolError(
                _MSG_MEDIA_TOO_LARGE.format(
                    kind="rendered PDF page",
                    path=f"{file_path}#page={exc.page_number}",
                    size=exc.size,
                    max_size=MAX_MEDIA_SIZE_BYTES,
                )
            ) from exc
        artifacts = await asyncio.gather(
            *(
                publish_media_artifact(
                    self.get_artifact_publisher(),
                    content=page.png,
                    representation="png",
                    kind="read-pdf-page",
                    mime_type="image/png",
                    suggested_name=(f"{os.path.basename(full_path)}-page-{page.page_number}.png"),
                )
                for page in publishable_pages
            )
        )
        return ToolResult(
            output="\n".join(descriptions),
            media=[  # type: ignore[arg-type]
                ToolMedia(
                    kind="image",
                    ref=f"{full_path}#page={page.page_number}",
                    mime="image/png",
                    artifact=artifact,
                )
                for page, artifact in zip(publishable_pages, artifacts, strict=True)
            ],
            resource_path=full_path,
            payload=json_tool_payload(data),
        )

    def _read_text_view(
        self,
        full_path: str,
        view: FileTextView,
    ) -> "str | ToolResult":
        cache_key = (
            view.mode.value,
            view.offset,
            len(view.lines),
            view.next_offset,
            view.snapshot.version.digest,
        )
        if self._read_state.get(full_path) == cache_key and self._prior_read_visible(full_path):
            self._mark_read(view.snapshot)
            return FILE_UNCHANGED_STUB

        self._read_state[full_path] = cache_key
        self._mark_read(view.snapshot)
        output, data = format_text_view(view)
        return ToolResult(
            output=output,
            resource_path=full_path,
            payload=json_tool_payload(data),
        )

    async def _read_image(self, file_path, full_path, ext, raw, detail) -> ToolResult:
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
        size = len(raw)
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
            final_bytes, note = prepare_image(
                raw,
                detail,
                max_dimension=MAX_IMAGE_DIMENSION,
            )
        except ImageProcessingError as exc:
            raise ToolError(_MSG_CANNOT_PROCESS_IMAGE.format(path=file_path, error=exc)) from exc
        mime_type = f"image/{'jpeg' if ext in {'jpg', 'jpeg'} else ext}"
        artifact = await publish_media_artifact(
            self.get_artifact_publisher(),
            content=final_bytes,
            representation=ext,
            kind="read-image",
            mime_type=mime_type,
            suggested_name=os.path.basename(full_path),
        )
        return ToolResult(
            output=_MSG_IMAGE_OUTPUT.format(path=file_path, ext=ext, size=size, note=note),
            media=[  # type: ignore[arg-type]
                ToolMedia(
                    kind="image",
                    ref=full_path,
                    mime=mime_type,
                    artifact=artifact,
                )
            ],
            payload=json_tool_payload(
                {
                    "type": "image",
                    "size": size,
                    "detail": detail,
                    "sent_bytes": len(final_bytes),
                }
            ),
        )

    async def _read_video(self, file_path, raw, extension) -> ToolResult:
        """Decode a local video into timestamped frames + a transcript.

        Frames are returned as supplemental image ``ToolMedia`` (a frame IS an
        image — Read's existing vision outlet) plus a text summary carrying the
        metadata and the timestamped transcript. The heavy decode runs in the
        shared ``_video`` kernel (ffmpeg/ffprobe); a missing tool raises
        :class:`ToolNotConfiguredError`, a decode failure a hard error.
        """
        try:
            result = await decompose_video_bytes(raw, extension, max_frames=_VIDEO_MAX_FRAMES)
        except VideoDecodeUnavailable as exc:
            raise ToolNotConfiguredError(_MSG_VIDEO_UNAVAILABLE.format(error=exc)) from exc
        except VideoDecodeFailed as exc:
            raise ToolError(_MSG_VIDEO_FAILED.format(path=file_path, error=exc)) from exc
        if not result.frames:
            raise ToolError(_MSG_VIDEO_NO_FRAMES.format(path=file_path))

        frames = [frame for frame in result.frames if len(frame.jpeg) <= MAX_MEDIA_SIZE_BYTES]
        artifacts = await asyncio.gather(
            *(
                publish_media_artifact(
                    self.get_artifact_publisher(),
                    content=frame.jpeg,
                    representation="jpeg",
                    kind="read-video-frame",
                    mime_type="image/jpeg",
                    suggested_name=f"{os.path.basename(file_path)}-{index:03d}.jpg",
                )
                for index, frame in enumerate(frames)
            )
        )
        media = [ToolMedia(kind="image", mime="image/jpeg", artifact=artifact) for artifact in artifacts]
        return ToolResult(
            output=video_summary(file_path, result, kept=len(media)),
            media=tuple(media),
            payload=json_tool_payload(
                {
                    "type": "video",
                    "frames": len(media),
                    "engine": result.engine,
                    "has_transcript": bool(result.transcript),
                }
            ),
        )

    async def _read_pdf(self, file_path, full_path, raw) -> ToolResult:
        """Read a PDF and return it as a supplemental document.

        Refuses up-front with :class:`ToolNotConfiguredError` when the main model
        does not accept native PDF input — an attached PDF would never reach it.
        """
        model = self._default_model()
        if model is not None and not supports_pdf_input(model):
            raise ToolNotConfiguredError(_MSG_PDF_MODEL_UNSUPPORTED.format(path=file_path, model=model))
        size = len(raw)
        if size > MAX_MEDIA_SIZE_BYTES:
            raise ToolError(
                _MSG_MEDIA_TOO_LARGE.format(
                    kind="PDF",
                    path=file_path,
                    size=size,
                    max_size=MAX_MEDIA_SIZE_BYTES,
                )
            )
        artifact = await publish_media_artifact(
            self.get_artifact_publisher(),
            content=raw,
            representation="pdf",
            kind="read-pdf",
            mime_type="application/pdf",
            suggested_name=os.path.basename(full_path),
        )
        return ToolResult(
            output=_MSG_PDF_OUTPUT.format(path=file_path, size=size),
            media=[  # type: ignore[arg-type]
                ToolMedia(
                    kind="pdf",
                    ref=full_path,
                    mime="application/pdf",
                    artifact=artifact,
                )
            ],
            payload=json_tool_payload({"type": "pdf", "size": size}),
        )

    def _read_notebook(self, file_path, raw) -> str:
        """Render a Jupyter notebook (.ipynb) as readable text."""
        try:
            output = parse_notebook(raw)
        except NotebookDecodeError as exc:
            raise ToolError(_MSG_CANNOT_READ.format(path=file_path, error=exc)) from exc
        except NotebookJsonError as exc:
            raise ToolError(_MSG_NOTEBOOK_INVALID_JSON.format(path=file_path, error=exc)) from exc
        return output or system_reminder(_MSG_NOTEBOOK_NO_CELLS)

    def cleanup_session(self, session_id: str) -> None:
        """Drop the per-session dedup cache."""
        self._read_state.clear()
