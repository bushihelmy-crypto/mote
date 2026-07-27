"""One ripgrep-backed candidate discovery implementation for Search and Edit."""

from __future__ import annotations

import os
import subprocess
import tempfile

from mote.contracts.fileops.errors import SearchDiscoveryError
from mote.runtime.fileops.identity import path_token
from mote.runtime.fileops.query_semantics import CandidateDiscovery, CandidateDiscoveryRequest
from mote.runtime.fileops.ripgrep import find_ripgrep

_VCS_DIRECTORIES = (".git", ".hg", ".svn")
_HEAVY_DIRECTORIES = (
    ".mypy_cache",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "site-packages",
    "venv",
)
_DOCUMENT_TYPES: dict[str, tuple[str, ...]] = {
    "pdf": (".pdf",),
    "docx": (".docx",),
    "word": (".docx",),
    "xlsx": (".xlsx",),
    "excel": (".xlsx",),
}


class CandidateDiscoveryService:
    """Freezes one canonical path set without opening candidate contents."""

    def discover(
        self,
        request: CandidateDiscoveryRequest,
        *,
        timeout: float,
    ) -> CandidateDiscovery:
        if not isinstance(request, CandidateDiscoveryRequest):
            raise TypeError("candidate discovery request is invalid")
        if type(timeout) not in (int, float) or isinstance(timeout, bool) or timeout <= 0:
            raise ValueError("candidate discovery timeout must be positive")
        root = request.root
        single_file = os.path.isfile(root.native)
        if not single_file and not os.path.isdir(root.native):
            raise SearchDiscoveryError(
                f"search root is not a directory or regular file: {root.display}",
                path=root.display,
            )
        discovery_root = (
            os.path.dirname(root.native) or (b"." if isinstance(root.native, bytes) else ".")
            if single_file
            else root.native
        )
        ripgrep = find_ripgrep()
        if ripgrep is None:
            raise SearchDiscoveryError("ripgrep is required for candidate discovery")
        args = [ripgrep, "--files", "--hidden", "--sort", "path", "-0"]
        if single_file:
            args.extend(("--max-depth", "1"))
        for directory in (*_VCS_DIRECTORIES, *_HEAVY_DIRECTORIES):
            args.extend(("--glob", f"!{directory}"))
        for pattern in request.globs:
            args.extend(("--glob", pattern))
        if request.type_name:
            if request.type_name in _DOCUMENT_TYPES:
                rg_type = f"mote-{request.type_name}"
                for extension in _DOCUMENT_TYPES[request.type_name]:
                    args.extend(
                        (
                            "--type-add",
                            f"{rg_type}:{_casefold_extension_glob(extension)}",
                        )
                    )
                args.extend(("--type", rg_type))
            else:
                args.extend(("--type", request.type_name))
        candidates = []
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            try:
                completed = subprocess.run(
                    args,
                    cwd=discovery_root,
                    stdout=stdout,
                    stderr=stderr,
                    timeout=timeout,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise SearchDiscoveryError(
                    "candidate discovery failed",
                    path=root.display,
                    cause=exc,
                ) from exc
            if completed.returncode not in {0, 1}:
                stderr.seek(0)
                detail = stderr.read(65_536).decode("utf-8", errors="backslashreplace").strip()
                raise SearchDiscoveryError(
                    detail or f"ripgrep candidate discovery exited {completed.returncode}",
                    path=root.display,
                )
            stdout.seek(0)
            pending = b""
            while True:
                chunk = stdout.read(64 * 1_024)
                if not chunk:
                    break
                fields = (pending + chunk).split(b"\0")
                pending = fields.pop()
                for raw_path in fields:
                    if not raw_path:
                        continue
                    candidate = path_token(os.path.join(discovery_root, os.fsdecode(raw_path)))
                    if single_file and candidate.native != root.native:
                        continue
                    candidates.append(candidate)
            if pending:
                raise SearchDiscoveryError(
                    "ripgrep returned an unterminated candidate path",
                    path=root.display,
                )
        return CandidateDiscovery.freeze(request, candidates)


def _casefold_extension_glob(extension: str) -> str:
    return "*" + "".join(
        f"[{character.lower()}{character.upper()}]" if character.isalpha() else character for character in extension
    )


__all__ = ["CandidateDiscoveryService"]
