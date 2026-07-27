"""Persistent code-map store location — a per-repo dir under ``~/.mote``.

Layer C's whole-repo index is persistent (it survives across sessions so a warm
start skips the full rescan) but lives *outside* the user's repo — their choice.
The DB path is a hash of the repo's realpath under the ``~/.mote`` convention
(``CONFIG_ROOT``), so two checkouts of the same tree share nothing and the path
stays stable across sessions for one repo::

    ~/.mote/codemap/<sha256(realpath(project_root))[:16]>/codemap.db

Depends only on ``common`` (``CONFIG_ROOT``) — safe for the low ``context`` layer.
"""

from __future__ import annotations

import hashlib
import os

from mote.runtime.paths import CONFIG_ROOT


def codemap_db_path(project_root: str) -> str:
    """Absolute path to the persistent code-map DB for *project_root*.

    Pure path derivation; the storage owner creates the directory when it opens
    the database. The directory name is a 16-hex-char sha256 prefix of
    the repo's realpath — collision-safe in practice, and stable for one repo.
    """
    real = os.path.realpath(project_root)
    digest = hashlib.sha256(real.encode("utf-8")).hexdigest()[:16]
    repo_dir = os.path.join(str(CONFIG_ROOT), "codemap", digest)
    return os.path.join(repo_dir, "codemap.db")


__all__ = ["codemap_db_path"]
