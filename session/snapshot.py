"""File-history snapshots — before-image capture for the session (Phase 1).

Claude Code's ``fileHistory`` synthesis: just before a file-mutating tool
(Write/Edit/NotebookEdit) overwrites a file, the *current* on-disk content is
captured as a content-addressed blob and a :class:`FileSnapshotEvent` is
appended to the same ``rollout.jsonl`` as the session's other events. That gives
a crash-safe, replayable file-history truth source for diff / undo / rollback,
without a second log.

Two pieces:

* :class:`BlobStore` — content-addressed (sha256) blob store under the session
  directory. Identical content is stored once (dedup); writes are atomic
  (tmp + ``os.replace``) so a crash never leaves a half-written blob masquerading
  as a valid one.
* :class:`FileSnapshotRecorder` — implements
  ``metagpt.common.interface.FileSnapshotStore``: reads the before-image, puts
  it in the blob store, appends the metadata event. Best-effort and never raises
  into the tool (a snapshot failure must not break a write). ``enabled`` gates
  recording (off during resume replay, mirroring
  :class:`~metagpt.session.subscribers.RecorderSubscriber`).

Only the *before* image is stored: that is what undo/rollback needs (the "after"
is whatever the tool just wrote, recoverable from the file itself or the next
snapshot). Post-image capture is intentionally out of scope.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from metagpt.common.disk import atomic_write, get_disk_writer
from metagpt.common.utils.git_state import find_git_root
from metagpt.common.logs import log_class, logger
from metagpt.session.events import FileSnapshotEvent
from metagpt.session.log import SessionLog

#: Directory (inside a session dir) holding content-addressed before-images.
BLOBS_DIRNAME = "blobs"
#: Directory (inside a session dir) holding the independent git object store.
GIT_DIRNAME = "git"


@log_class(level="DEBUG", exclude={"path", "exists"})
class BlobStore:
    """Content-addressed (sha256) blob store under a session directory.

    Blobs live at ``{session_dir}/blobs/{hash[:2]}/{hash}``; the two-char shard
    keeps any single directory from growing unbounded. ``put`` is idempotent: an
    already-present hash is a no-op, so identical before-images across many
    edits are stored exactly once.
    """

    #: Backend tag stored in the event so the read side knows how to fetch.
    name = "blob"

    def __init__(self, base_dir: Path):
        self._dir = Path(base_dir) / BLOBS_DIRNAME

    def _path(self, digest: str) -> Path:
        return self._dir / digest[:2] / digest

    def exists(self, digest: str) -> bool:
        return self._path(digest).exists()

    def put(self, content: bytes) -> str:
        """Store ``content`` and return its sha256 hex digest (dedup on existing).

        The digest is computed in memory, so the hash is returned immediately
        (no disk wait); the actual blob write is ordered through the shared
        :class:`~metagpt.common.disk.DiskWriter` (per-blob key) using
        :func:`~metagpt.common.disk.atomic_write`. A reader therefore sees either
        the old absence or the complete blob — but should ``drain`` first if it
        needs the bytes (see :mod:`metagpt.session.history`). Idempotent: an
        already-present hash is a no-op.
        """
        digest = hashlib.sha256(content).hexdigest()
        dest = self._path(digest)
        if dest.exists():
            return digest  # dedup: identical content already stored
        get_disk_writer().enqueue(str(dest), lambda: atomic_write(dest, content))
        return digest

    def get(self, digest: str) -> Optional[bytes]:
        """Return the stored bytes for ``digest``, or ``None`` if absent."""
        try:
            return self._path(digest).read_bytes()
        except OSError:
            return None


@log_class(level="DEBUG", exclude={"exists"})
class GitBlobStore:
    """Content-addressed store backed by an independent git object database.

    Reuses git's loose-object store (zlib-compressed, content-addressed by the
    git blob id) as the snapshot backend for code workspaces — cheaper on disk
    than raw before-image blobs, and inspectable / pack-able with the git
    plumbing. The object db is a **dedicated bare** ``{session_dir}/git`` repo,
    deliberately NOT the user's project repo: this keeps our before-images from
    polluting the user's history and, crucially, from being deleted by a user's
    ``git gc --prune`` (a dangling object would otherwise be reaped, breaking
    restore). Keys are git blob ids (sha1), opaque to the rest of the system.

    All operations shell out to ``git`` (the binary's presence is a precondition
    — see :func:`detect_blob_backend`). Failures degrade to ``None``/``False`` so
    the recorder's best-effort contract holds.
    """

    name = "git"

    def __init__(self, base_dir: Path):
        self._git_dir = Path(base_dir) / GIT_DIRNAME

    def _ensure_repo(self) -> None:
        if not (self._git_dir / "HEAD").exists():
            self._git_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "init", "--bare", "--quiet", str(self._git_dir)],
                check=True,
                capture_output=True,
            )

    def _run(self, args: list[str], *, input_bytes: Optional[bytes] = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "--git-dir", str(self._git_dir), *args],
            input=input_bytes,
            capture_output=True,
        )

    def put(self, content: bytes) -> str:
        """Write ``content`` as a git blob object and return its id (sha1 hex)."""
        self._ensure_repo()
        proc = self._run(["hash-object", "-w", "--stdin"], input_bytes=content)
        if proc.returncode != 0:
            raise OSError(f"git hash-object failed: {proc.stderr.decode('utf-8', 'replace').strip()}")
        return proc.stdout.decode("ascii").strip()

    def exists(self, digest: str) -> bool:
        if not (self._git_dir / "HEAD").exists():
            return False
        return self._run(["cat-file", "-e", digest]).returncode == 0

    def get(self, digest: str) -> Optional[bytes]:
        """Return the bytes of git blob ``digest``, or ``None`` if absent."""
        if not (self._git_dir / "HEAD").exists():
            return None
        proc = self._run(["cat-file", "blob", digest])
        return proc.stdout if proc.returncode == 0 else None


def detect_blob_backend(working_dir: Optional[str] = None) -> str:
    """Pick the snapshot backend for ``working_dir`` ("git" or "blob").

    Heuristic mirroring the design intent: a code workspace (the cwd sits inside
    a git work tree) with the ``git`` binary available uses the ``git`` backend;
    everything else (non-code tasks, no git, errors) uses the plain ``blob``
    backend. Best-effort — any failure falls back to ``"blob"``.

    The "are we in a repo?" probe reuses :func:`metagpt.common.utils.git_state.find_git_root`
    (filesystem-first, no subprocess, handles ``.git`` file pointers). The
    ``git`` binary must still be present because :class:`GitBlobStore` shells out
    to it for every put/get.
    """
    if shutil.which("git") is None:
        return "blob"
    try:
        if find_git_root(working_dir or os.getcwd()) is not None:
            return "git"
    except Exception:  # noqa: BLE001 — best-effort; never break recorder setup
        pass
    return "blob"


def make_blob_store(base_dir: Path, backend: str = "blob"):
    """Construct the blob store for ``backend`` ("git" -> git object db, else blob)."""
    if backend == "git":
        return GitBlobStore(base_dir)
    return BlobStore(base_dir)


@log_class(level="DEBUG", exclude={"snapshot"})
class FileSnapshotRecorder:
    """Captures before-images of mutated files into the session log + blob store.

    Conforms to ``metagpt.common.interface.FileSnapshotStore``. Shares the
    session's :class:`SessionLog` so snapshot events interleave with the rest of
    the rollout, and owns a content-addressed store rooted at the same session
    dir. ``backend`` selects the store implementation ("blob" = raw blob files,
    "git" = git object db); the chosen backend tag is stamped on each event so
    the read side (:mod:`history`) knows how to fetch the content back.
    """

    def __init__(self, log: SessionLog, *, enabled: bool = True, backend: str = "blob"):
        self._log = log
        self._store = make_blob_store(log.path.parent, backend)
        self.enabled = enabled

    @property
    def log(self) -> SessionLog:
        return self._log

    @property
    def blobs(self):
        """The content-addressed store (BlobStore or GitBlobStore)."""
        return self._store

    def snapshot(self, full_path: str, *, tool: str = "") -> None:
        """Record the current on-disk content of ``full_path`` as a before-image.

        A missing file is recorded as a ``create`` (``pre_hash=None``); an
        existing file's content is stored in the blob store and referenced by
        hash. Best-effort: any failure is logged and swallowed so the tool's
        write still proceeds.
        """
        if not self.enabled:
            return
        try:
            try:
                content = Path(full_path).read_bytes()
            except FileNotFoundError:
                self._log.append(
                    FileSnapshotEvent(
                        path=full_path, operation="create", tool=tool, backend=self._store.name
                    )
                )
                return
            except OSError as exc:
                # Unreadable (e.g. a directory or permission issue): the tool's
                # own write attempt will surface the real error. Skip snapshot.
                logger.warning(f"FileSnapshotRecorder: cannot read '{full_path}': {exc}")
                return

            digest = self._store.put(content)
            self._log.append(
                FileSnapshotEvent(
                    path=full_path,
                    operation="update",
                    pre_hash=digest,
                    pre_size=len(content),
                    tool=tool,
                    backend=self._store.name,
                )
            )
        except Exception as exc:  # noqa: BLE001 — snapshotting must not break a write
            logger.warning(f"FileSnapshotRecorder: failed to snapshot '{full_path}': {exc}")


__all__ = [
    "BlobStore",
    "GitBlobStore",
    "FileSnapshotRecorder",
    "make_blob_store",
    "detect_blob_backend",
    "BLOBS_DIRNAME",
    "GIT_DIRNAME",
]
