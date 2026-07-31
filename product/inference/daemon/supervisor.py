"""Single-owner Shared Process supervisor and safe UDS discovery lifecycle."""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import socket
import stat
import struct
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import IO, Any

from mote.product.inference.daemon.security import SharedAuthenticationError, current_incarnation


class DaemonState(StrEnum):
    ABSENT = "absent"
    STARTING = "starting"
    READY = "ready"
    DRAINING = "draining"
    STOPPED = "stopped"
    CRASHED = "crashed"
    RECONCILING = "reconciling"


@dataclass(frozen=True, slots=True)
class DaemonDiscovery:
    schema_version: int
    socket_generation: str
    socket_path: str
    pid: int
    process_start_ticks: int
    boot_id: str
    protocol_version: int
    state: str


class SupervisorOwnershipError(RuntimeError):
    pass


class SharedDaemonSupervisor:
    def __init__(
        self,
        runtime_directory: Path,
        *,
        protocol_version: int,
    ) -> None:
        if not runtime_directory.is_absolute() or protocol_version < 2:
            raise ValueError("Shared runtime path and protocol version are invalid")
        self._directory = runtime_directory
        self._protocol_version = protocol_version
        self._lock_path = runtime_directory / "gateway.lock"
        self._discovery_path = runtime_directory / "gateway.json"
        self._lock_file: IO[bytes] | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._state = DaemonState.ABSENT

    @property
    def state(self) -> DaemonState:
        return self._state

    def acquire(self) -> None:
        self._ensure_directory()
        lock_file = self._lock_path.open("a+b")
        os.chmod(self._lock_path, 0o600)
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock_file.close()
            raise SupervisorOwnershipError("Shared daemon lock is already held") from exc
        self._lock_file = lock_file

    def release(self) -> None:
        if self._lock_file is None:
            return
        fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
        self._lock_file.close()
        self._lock_file = None

    def prepare_generation(self) -> tuple[str, Path]:
        self._require_lock()
        self._handle_stale_discovery()
        generation = secrets.token_hex(12)
        socket_path = self._directory / f"gateway-{generation}.sock"
        if len(os.fsencode(socket_path)) >= 104:
            raise ValueError("Shared UDS path exceeds portable Unix socket limit")
        self._state = DaemonState.STARTING
        return generation, socket_path

    def launch(
        self,
        command: tuple[str, ...],
        *,
        socket_generation: str,
        socket_path: Path,
        bootstrap_payload: bytes | None = None,
    ) -> subprocess.Popen[bytes]:
        self._require_lock()
        if self._state is not DaemonState.STARTING or not command:
            raise RuntimeError("supervisor is not prepared to launch")
        environment = os.environ.copy()
        environment["MOTE_SHARED_SOCKET_GENERATION"] = socket_generation
        environment["MOTE_SHARED_SOCKET_PATH"] = str(socket_path)
        parent_socket: socket.socket | None = None
        child_socket: socket.socket | None = None
        pass_fds: tuple[int, ...] = ()
        if bootstrap_payload is not None:
            if not bootstrap_payload or len(bootstrap_payload) > 1024 * 1024:
                raise ValueError("Shared bootstrap payload size is invalid")
            parent_socket, child_socket = socket.socketpair(
                socket.AF_UNIX,
                socket.SOCK_STREAM,
            )
            child_socket.set_inheritable(True)
            environment["MOTE_SHARED_BOOTSTRAP_FD"] = str(child_socket.fileno())
            pass_fds = (child_socket.fileno(),)
        try:
            self._process = subprocess.Popen(
                command,
                cwd=str(self._directory),
                env=environment,
                close_fds=True,
                pass_fds=pass_fds,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if parent_socket is not None and bootstrap_payload is not None:
                parent_socket.sendall(struct.pack(">I", len(bootstrap_payload)) + bootstrap_payload)
        finally:
            if child_socket is not None:
                child_socket.close()
            if parent_socket is not None:
                parent_socket.close()
        return self._process

    def publish_ready(
        self,
        *,
        socket_generation: str,
        socket_path: Path,
        pid: int,
    ) -> DaemonDiscovery:
        self._require_lock()
        if not _probe_socket(socket_path):
            raise RuntimeError("daemon socket is not accepting connections")
        info = socket_path.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise PermissionError("daemon socket ownership or mode is unsafe")
        incarnation = current_incarnation(pid)
        discovery = DaemonDiscovery(
            schema_version=1,
            socket_generation=socket_generation,
            socket_path=str(socket_path),
            pid=pid,
            process_start_ticks=incarnation.process_start_ticks,
            boot_id=incarnation.boot_id,
            protocol_version=self._protocol_version,
            state=DaemonState.READY.value,
        )
        self._atomic_write(self._discovery_path, asdict(discovery))
        self._state = DaemonState.READY
        return discovery

    def publish_state(self, state: DaemonState) -> None:
        self._require_lock()
        discovery = self.read_discovery()
        if discovery is None:
            raise RuntimeError("daemon discovery does not exist")
        payload = asdict(discovery)
        payload["state"] = state.value
        self._atomic_write(self._discovery_path, payload)
        self._state = state

    def read_discovery(self) -> DaemonDiscovery | None:
        try:
            info = self._discovery_path.stat()
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise PermissionError("Shared discovery ownership or mode is unsafe")
            payload = json.loads(self._discovery_path.read_text(encoding="utf-8"))
            return DaemonDiscovery(**payload)
        except FileNotFoundError:
            return None

    def discover_ready_socket(self) -> tuple[DaemonDiscovery, Path]:
        discovery = self.read_discovery()
        if discovery is None or discovery.state != DaemonState.READY.value:
            raise RuntimeError("Shared daemon has no READY discovery record")
        if discovery.protocol_version not in {
            self._protocol_version,
            self._protocol_version - 1,
        }:
            raise RuntimeError("Shared daemon discovery protocol is incompatible")
        socket_path = Path(discovery.socket_path)
        if socket_path.parent != self._directory or not socket_path.name.startswith(
            f"gateway-{discovery.socket_generation}"
        ):
            raise PermissionError("Shared discovery socket escaped runtime directory")
        if not _incarnation_matches(discovery) or not _probe_socket(socket_path):
            raise RuntimeError("Shared discovery points to a stale daemon")
        info = socket_path.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise PermissionError("Shared discovery socket ownership or mode is unsafe")
        return discovery, socket_path

    def _handle_stale_discovery(self) -> None:
        discovery = self.read_discovery()
        if discovery is None:
            return
        socket_path = Path(discovery.socket_path)
        process_alive = _incarnation_matches(discovery)
        socket_alive = _probe_socket(socket_path)
        if process_alive or socket_alive:
            raise SupervisorOwnershipError("an existing Shared daemon is still live")
        if socket_path.exists() or socket_path.is_socket():
            stale = self._directory / (f"{socket_path.name}.stale-{secrets.token_hex(6)}")
            os.replace(socket_path, stale)
        stale_discovery = self._directory / (f"gateway.json.stale-{secrets.token_hex(6)}")
        os.replace(self._discovery_path, stale_discovery)

    def _ensure_directory(self) -> None:
        if self._directory.exists() and self._directory.is_symlink():
            raise PermissionError("Shared runtime directory cannot be a symlink")
        self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self._directory.stat()
        if info.st_uid != os.getuid():
            raise PermissionError("Shared runtime directory owner mismatch")
        os.chmod(self._directory, 0o700)

    def _require_lock(self) -> None:
        if self._lock_file is None:
            raise SupervisorOwnershipError("Shared daemon lock is not held")

    @staticmethod
    def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


def _incarnation_matches(discovery: DaemonDiscovery) -> bool:
    try:
        current = current_incarnation(discovery.pid)
    except SharedAuthenticationError:
        return False
    return current.process_start_ticks == discovery.process_start_ticks and current.boot_id == discovery.boot_id


def _probe_socket(path: Path, *, timeout_seconds: float = 0.2) -> bool:
    if not path.exists():
        return False
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout_seconds)
    try:
        client.connect(str(path))
    except OSError:
        return False
    finally:
        client.close()
    return True
