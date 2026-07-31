"""Stable facade for Product defaults and ``.mote`` discovery."""

from mote.product.paths.defaults import MOTE_PACKAGE_DIR, default_runtime_paths
from mote.product.paths.discovery import (
    MOTE_DIR_NAME,
    mote_layered_files,
    mote_project_dirs,
    mote_project_files,
    mote_source_dirs,
    user_mote_dir,
)
from mote.product.paths.model import RuntimePaths

__all__ = [
    "MOTE_DIR_NAME",
    "MOTE_PACKAGE_DIR",
    "RuntimePaths",
    "default_runtime_paths",
    "mote_layered_files",
    "mote_project_dirs",
    "mote_project_files",
    "mote_source_dirs",
    "user_mote_dir",
]
