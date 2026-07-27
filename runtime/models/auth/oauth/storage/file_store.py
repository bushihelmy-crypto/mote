#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""File-backed credential store at ``~/.mote/oauth/<provider>.json`` (0600)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from mote.runtime.disk import mtime_seconds
from mote.runtime.models.auth.oauth.models import OAuthToken
from mote.runtime.models.auth.oauth.storage.base import CredentialStore
from mote.runtime.paths import CONFIG_ROOT

OAUTH_DIR = CONFIG_ROOT / "oauth"


class FileCredentialStore(CredentialStore):
    """Stores the token as JSON in the user's ``~/.mote/oauth`` directory.

    The file is created with ``0600`` permissions (owner read/write only) so the
    bearer/refresh tokens are not world-readable.
    """

    def __init__(self, provider: str, base_dir: Optional[Path] = None) -> None:
        super().__init__(provider)
        self._dir = Path(base_dir) if base_dir is not None else OAUTH_DIR
        self._path = self._dir / f"{provider}.json"

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> Optional[OAuthToken]:
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - corrupt/partial file => treat as absent
            return None
        return OAuthToken(**data)

    def save(self, token: OAuthToken) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        # Best-effort: lock down the directory too.
        try:
            os.chmod(self._dir, 0o700)
        except OSError:
            pass
        payload = json.dumps(token.model_dump(), ensure_ascii=False, indent=2)
        # Write then chmod to 0600. Create with restrictive mode from the start
        # by opening the fd with mode 0o600 to avoid a readable window.
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
        finally:
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                pass

    def delete(self) -> None:
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass

    def mtime(self) -> Optional[float]:
        return mtime_seconds(self._path)
