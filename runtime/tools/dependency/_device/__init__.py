"""DeviceUse dependency layer — backend-agnostic device session + pluggable backends.

Layers (mirroring ``mote.runtime.sandbox``'s façade + backend split):

* :mod:`outline` — backend-agnostic a11y outline model, ``@e{N}`` ref
  stabilization, compact rendering, cross-snapshot diff.
* :mod:`backend` — the :class:`DeviceBackend` strategy contract + a null backend
  + :func:`select_device_backend`.
* :mod:`android_adb` — the first concrete backend (adb-reachable Android).
* :mod:`session` — :class:`DeviceSession`, the per-Role live handle that owns a
  backend + the latest snapshot + a serialization lock.
* :mod:`runtime` — :class:`DeviceRuntimeDriver`, the shared RuntimeHost adapter.
"""
from __future__ import annotations
