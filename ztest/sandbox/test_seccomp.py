#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the seccomp BPF layer.

Two tiers:
  * pure unit tests for filter-building / file export / degradation (run wherever
    pyseccomp is importable);
  * end-to-end enforcement tests behind a ``skipif`` requiring BOTH a usable
    seccomp toolchain AND a bwrap binary (the BPF is only meaningful once bwrap
    attaches it via ``--seccomp FD``).
"""
from __future__ import annotations

import asyncio
import os
import shutil

import pytest
from mote.sandbox import seccomp
from mote.sandbox.runtime import SandboxRuntime

_HAS_SECCOMP = seccomp.seccomp_available()
_HAS_BWRAP = shutil.which("bwrap") is not None and os.name == "posix"
_E2E = _HAS_SECCOMP and _HAS_BWRAP


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.mark.skipif(not _HAS_SECCOMP, reason="pyseccomp/libseccomp not available")
class TestBuildFilters:
    def test_hardening_filter_exports_file(self):
        path = seccomp.build_hardening_filter()
        try:
            assert path is not None
            assert os.path.exists(path)
            # Non-empty compiled BPF program.
            assert os.path.getsize(path) > 0
            # Created 0600.
            assert (os.stat(path).st_mode & 0o777) == 0o600
        finally:
            if path:
                os.unlink(path)

    def test_block_inet_filter_exports_file(self):
        path = seccomp.build_block_inet_filter()
        try:
            assert path is not None
            assert os.path.exists(path)
            assert os.path.getsize(path) > 0
        finally:
            if path:
                os.unlink(path)

    def test_hardening_denylist_all_resolvable(self):
        # Every curated name should resolve on this arch (the module filters
        # unknown names; a regression here means a typo'd syscall name).
        import pyseccomp as s

        resolved = seccomp._resolve_known(s, seccomp._HARDENING_DENY)
        assert set(resolved) == set(seccomp._HARDENING_DENY)


class TestAvailabilityDegradation:
    def test_available_is_bool(self):
        assert isinstance(seccomp.seccomp_available(), bool)

    def test_build_returns_none_when_unavailable(self, monkeypatch):
        monkeypatch.setattr(seccomp, "seccomp_available", lambda: False)
        assert seccomp.build_hardening_filter() is None
        assert seccomp.build_block_inet_filter() is None

    def test_export_failure_returns_none(self, monkeypatch):
        # Force mkstemp to raise so _export_to_file takes the error path.
        import tempfile

        def boom(*a, **k):
            raise OSError("nope")

        monkeypatch.setattr(tempfile, "mkstemp", boom)

        class _Filt:
            def export_bpf(self, fh):  # pragma: no cover - never reached
                pass

        assert seccomp._export_to_file(_Filt()) is None


@pytest.mark.skipif(not _HAS_SECCOMP, reason="pyseccomp/libseccomp not available")
class TestRuntimeBuildsFilter:
    def test_start_builds_bpf_when_enabled(self):
        rt = SandboxRuntime(backend="none", seccomp=True, network="open")
        _run(rt.start())
        # NullBackend has no --seccomp to carry the filter, so no BPF is built.
        assert rt._seccomp_bpf_path is None
        _run(rt.shutdown())

    @pytest.mark.skipif(not _HAS_BWRAP, reason="bwrap not installed")
    def test_start_builds_bpf_with_bwrap(self):
        rt = SandboxRuntime(backend="bwrap", seccomp=True, network="open")
        _run(rt.start())
        try:
            assert rt._seccomp_bpf_path is not None
            assert os.path.exists(rt._seccomp_bpf_path)
        finally:
            path = rt._seccomp_bpf_path
            _run(rt.shutdown())
            # Shutdown unlinks the BPF + clears the slot.
            assert rt._seccomp_bpf_path is None
            assert not os.path.exists(path)

    def test_disabled_builds_no_bpf(self):
        rt = SandboxRuntime(backend="bwrap", seccomp=False, network="open")
        _run(rt.start())
        assert rt._seccomp_bpf_path is None
        _run(rt.shutdown())


@pytest.mark.skipif(not _E2E, reason="needs bwrap + seccomp for real enforcement")
class TestSeccompEnforcement:
    """The BPF actually denies dangerous syscalls inside the sandbox."""

    def _exec(self, wrapped: str, env: dict):
        async def go():
            proc = await asyncio.create_subprocess_shell(
                wrapped,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            out, err = await proc.communicate()
            return proc.returncode, out.decode(), err.decode()

        return _run(go())

    def test_ptrace_denied(self, tmp_path):
        rt = SandboxRuntime(backend="bwrap", seccomp=True, harden_process=True, network="open")
        # ptrace(PTRACE_TRACEME=0) -> EPERM (errno 1) under the hardening filter.
        code = (
            "import ctypes; "
            "libc=ctypes.CDLL(None,use_errno=True); "
            "libc.ptrace(0,0,0,0); "
            "print('errno', ctypes.get_errno())"
        )
        cmd, env = _run(rt.wrap_command(f"python3 -c {_q(code)}", cwd=str(tmp_path), env=dict(os.environ)))
        rc, out, err = self._exec(cmd, env)
        _run(rt.shutdown())
        assert rc == 0, err
        assert "errno 1" in out  # EPERM

    def test_inner_command_runs_normally(self, tmp_path):
        # The hardening filter is default-ALLOW; ordinary commands still work.
        rt = SandboxRuntime(backend="bwrap", seccomp=True, harden_process=True, network="open")
        cmd, env = _run(rt.wrap_command("echo seccomp-ok", cwd=str(tmp_path), env=dict(os.environ)))
        rc, out, err = self._exec(cmd, env)
        _run(rt.shutdown())
        assert rc == 0, err
        assert "seccomp-ok" in out

    def test_wrap_exec_carries_seccomp(self, tmp_path):
        # The PTY/exec seam wraps bwrap in a sh-shim that redirects the BPF fd.
        rt = SandboxRuntime(backend="bwrap", seccomp=True, harden_process=False, network="open")
        argv, env = _run(rt.wrap_exec(["/bin/sh", "-c", "echo exec-ok"], cwd=str(tmp_path), env=dict(os.environ)))

        async def go():
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            out, err = await proc.communicate()
            return proc.returncode, out.decode(), err.decode()

        rc, out, err = _run(go())
        _run(rt.shutdown())
        assert rc == 0, err
        assert "exec-ok" in out


def _q(s: str) -> str:
    import shlex

    return shlex.quote(s)
