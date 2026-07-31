import hashlib
import os
import shutil
import socket
import struct
import subprocess
from dataclasses import asdict

import pytest

from mote.product.inference.daemon.bootstrap import read_inherited_bootstrap
from mote.product.inference.daemon.security import current_incarnation
from mote.product.inference.daemon.supervisor import DaemonDiscovery, SharedDaemonSupervisor, SupervisorOwnershipError


def test_supervisor_publishes_only_live_owner_only_socket(tmp_path):
    directory = _short_directory(tmp_path, "live")
    supervisor = SharedDaemonSupervisor(directory, protocol_version=3)
    supervisor.acquire()
    generation, socket_path = supervisor.prepare_generation()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    os.chmod(socket_path, 0o600)
    server.listen()
    try:
        discovery = supervisor.publish_ready(
            socket_generation=generation,
            socket_path=socket_path,
            pid=os.getpid(),
        )
        assert discovery.socket_generation == generation
        assert supervisor.read_discovery() == discovery
        contender = SharedDaemonSupervisor(directory, protocol_version=3)
        with pytest.raises(SupervisorOwnershipError, match="already held"):
            contender.acquire()
    finally:
        server.close()
        supervisor.release()
        shutil.rmtree(directory)


def test_stale_socket_moves_aside_only_under_lock_after_both_checks_fail(tmp_path):
    directory = _short_directory(tmp_path, "stale")
    supervisor = SharedDaemonSupervisor(directory, protocol_version=3)
    supervisor.acquire()
    generation, socket_path = supervisor.prepare_generation()
    stale_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale_socket.bind(str(socket_path))
    stale_socket.close()
    os.chmod(socket_path, 0o600)
    incarnation = current_incarnation(os.getpid())
    dead = DaemonDiscovery(
        schema_version=1,
        socket_generation=generation,
        socket_path=str(socket_path),
        pid=999_999_999,
        process_start_ticks=incarnation.process_start_ticks,
        boot_id=incarnation.boot_id,
        protocol_version=3,
        state="ready",
    )
    supervisor._atomic_write(directory / "gateway.json", asdict(dead))
    next_generation, next_socket = supervisor.prepare_generation()
    assert next_generation != generation
    assert next_socket != socket_path
    assert not socket_path.exists()
    assert tuple(directory.glob("gateway-*.sock.stale-*"))
    supervisor.release()
    shutil.rmtree(directory)


def _short_directory(tmp_path, suffix):
    digest = hashlib.sha256(str(tmp_path).encode()).hexdigest()[:8]
    return tmp_path.parent.parent.parent / f"mote-{digest}-{suffix}"


def test_bootstrap_secret_uses_inherited_fd_not_command_or_environment(tmp_path, monkeypatch):
    directory = _short_directory(tmp_path, "bootstrap")
    supervisor = SharedDaemonSupervisor(directory, protocol_version=3)
    supervisor.acquire()
    generation, socket_path = supervisor.prepare_generation()
    observed = {}

    class Process:
        pid = os.getpid()

    def launch(command, **kwargs):
        observed["command"] = command
        observed["environment"] = kwargs["env"]
        observed["pass_fds"] = kwargs["pass_fds"]
        descriptor = kwargs["pass_fds"][0]
        duplicate = os.dup(descriptor)
        observed["duplicate"] = duplicate
        return Process()

    monkeypatch.setattr(subprocess, "Popen", launch)
    secret = b"credential-material"
    supervisor.launch(
        ("mote-shared-daemon",),
        socket_generation=generation,
        socket_path=socket_path,
        bootstrap_payload=secret,
    )
    descriptor = observed["duplicate"]
    channel = socket.socket(fileno=descriptor)
    try:
        size = struct.unpack(">I", channel.recv(4))[0]
        assert channel.recv(size) == secret
    finally:
        channel.close()
    assert secret.decode() not in " ".join(observed["command"])
    assert secret.decode() not in repr(observed["environment"])
    assert len(observed["pass_fds"]) == 1
    supervisor.release()
    shutil.rmtree(directory)
