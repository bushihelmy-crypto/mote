"""Synchronous serialization for durable read-validate-write transactions."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager


class SerialTransactionGate:
    """Serialize one owner's synchronous durable transactions.

    The gate owns only mutual exclusion. Callers retain ownership of transaction
    scope, domain validation, persistence format, and failure semantics.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            yield


__all__ = ["SerialTransactionGate"]
