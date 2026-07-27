#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the cgroup resource-limit builders + probes (``sandbox.resources``).

Two layers:
  * pure builders / dataclass + best-effort host probes (no systemd needed) —
    ``ResourceLimits.is_empty``, ``systemd_run_prefix`` shape, the cpu gate, and
    that the probes always return a bool without raising;
  * end-to-end group-level enforcement behind
    ``skipif(not cgroup_limits_available())`` — a memory bomb is OOM-killed and a
    fork loop is capped, exercised through a real ``SandboxRuntime``.

All async exercised via ``asyncio.run`` (no pytest-asyncio).
"""
from __future__ import annotations

import asyncio

import pytest

from mote.runtime.sandbox import resources
from mote.runtime.sandbox.resources import (
    ResourceLimits,
    cgroup_limits_available,
    cpu_controller_delegated,
    rlimit_prelude,
    systemd_run_prefix,
)
from mote.runtime.sandbox.runtime import SandboxRuntime

_HAS_CGROUP = cgroup_limits_available()


def _run(coro):
    return asyncio.run(coro)


class TestResourceLimits:
    def test_empty_by_default(self):
        assert ResourceLimits().is_empty is True

    def test_swap_alone_is_empty(self):
        # memory_swap_max has a non-None default; on its own it's still a no-op
        # (it only matters alongside memory_max).
        assert ResourceLimits(memory_swap_max="0").is_empty is True

    def test_memory_makes_nonempty(self):
        assert ResourceLimits(memory_max="4G").is_empty is False

    def test_pids_makes_nonempty(self):
        assert ResourceLimits(pids_max=512).is_empty is False

    def test_cpu_makes_nonempty(self):
        assert ResourceLimits(cpu_quota="200%").is_empty is False


class TestSystemdRunPrefix:
    def test_empty_limits_no_prefix(self):
        assert systemd_run_prefix(ResourceLimits(), with_cpu=False) == []

    def test_scope_header(self):
        prefix = systemd_run_prefix(ResourceLimits(memory_max="4G"), with_cpu=False)
        assert prefix[:4] == ["systemd-run", "--user", "--scope", "--quiet"]

    def test_memory_and_swap(self):
        prefix = systemd_run_prefix(ResourceLimits(memory_max="512M"), with_cpu=False)
        assert "MemoryMax=512M" in prefix
        # Swap disabled alongside the memory cap so it can't be sidestepped.
        assert "MemorySwapMax=0" in prefix

    def test_swap_omitted_when_no_memory(self):
        prefix = systemd_run_prefix(ResourceLimits(pids_max=8), with_cpu=False)
        assert not any(a.startswith("MemorySwapMax") for a in prefix)
        assert not any(a.startswith("MemoryMax") for a in prefix)

    def test_pids_becomes_tasksmax(self):
        prefix = systemd_run_prefix(ResourceLimits(pids_max=64), with_cpu=False)
        assert "TasksMax=64" in prefix

    def test_cpu_omitted_when_not_delegated(self):
        prefix = systemd_run_prefix(ResourceLimits(cpu_quota="200%"), with_cpu=False)
        assert not any(a.startswith("CPUQuota") for a in prefix)

    def test_cpu_emitted_when_delegated(self):
        prefix = systemd_run_prefix(ResourceLimits(cpu_quota="200%"), with_cpu=True)
        assert "CPUQuota=200%" in prefix

    def test_cpu_absent_even_when_delegated_if_unset(self):
        # with_cpu=True but no cpu_quota requested -> nothing to emit.
        prefix = systemd_run_prefix(ResourceLimits(memory_max="1G"), with_cpu=True)
        assert not any(a.startswith("CPUQuota") for a in prefix)

    def test_full_set(self):
        prefix = systemd_run_prefix(
            ResourceLimits(memory_max="4G", pids_max=512, cpu_quota="100%"),
            with_cpu=True,
        )
        assert "MemoryMax=4G" in prefix
        assert "MemorySwapMax=0" in prefix
        assert "TasksMax=512" in prefix
        assert "CPUQuota=100%" in prefix


class TestRlimitPrelude:
    """The per-process ``ulimit`` fallback (weaker cgroup analogue).

    Maps memory_max -> ``ulimit -v <kb>`` (RLIMIT_AS) and pids_max -> ``ulimit
    -u`` (RLIMIT_NPROC). cpu_quota has no rate equivalent so it's dropped.
    """

    def test_empty_limits_no_prelude(self):
        assert rlimit_prelude(ResourceLimits()) == ""

    def test_memory_becomes_ulimit_v_in_kb(self):
        # 64M -> 65536 KiB.
        assert "ulimit -v 65536" in rlimit_prelude(ResourceLimits(memory_max="64M"))

    def test_gigabyte_memory_converts(self):
        # 4G -> 4194304 KiB.
        assert "ulimit -v 4194304" in rlimit_prelude(ResourceLimits(memory_max="4G"))

    def test_pids_becomes_ulimit_u(self):
        assert "ulimit -u 8" in rlimit_prelude(ResourceLimits(pids_max=8))

    def test_cpu_quota_dropped(self):
        # No ulimit rate equivalent; a cpu-only limit set maps to nothing.
        assert "ulimit" not in rlimit_prelude(ResourceLimits(cpu_quota="200%"))

    def test_best_effort_guarded(self):
        # Each ulimit is guarded so a refused cap never aborts the command.
        prelude = rlimit_prelude(ResourceLimits(memory_max="64M", pids_max=8))
        assert prelude.count("|| true") == 2

    def test_unparseable_memory_skipped(self):
        # A garbage byte spec drops the -v line rather than emitting garbage;
        # the pids cap still applies.
        prelude = rlimit_prelude(ResourceLimits(memory_max="notabyte", pids_max=8))
        assert "ulimit -v" not in prelude
        assert "ulimit -u 8" in prelude

    def test_full_set(self):
        prelude = rlimit_prelude(ResourceLimits(memory_max="512M", pids_max=64, cpu_quota="100%"))
        assert "ulimit -v 524288" in prelude
        assert "ulimit -u 64" in prelude
        assert "CPUQuota" not in prelude  # cpu has no ulimit form


class TestParseBytesToKb:
    def test_suffixes(self):
        assert resources._parse_bytes_to_kb("1K") == 1
        assert resources._parse_bytes_to_kb("1M") == 1024
        assert resources._parse_bytes_to_kb("1G") == 1024 * 1024

    def test_raw_bytes_floor_divided(self):
        # No suffix -> raw bytes, floored to KiB.
        assert resources._parse_bytes_to_kb("2048") == 2

    def test_garbage_returns_none(self):
        assert resources._parse_bytes_to_kb("xyz") is None
        assert resources._parse_bytes_to_kb("") is None


class TestProbes:
    def test_cgroup_limits_available_returns_bool(self):
        assert isinstance(cgroup_limits_available(), bool)

    def test_cpu_controller_delegated_returns_bool(self):
        assert isinstance(cpu_controller_delegated(), bool)

    def test_cpu_delegated_false_on_missing_path(self, monkeypatch):
        monkeypatch.setattr(resources, "_user_service_subtree_control", lambda: None)
        assert cpu_controller_delegated() is False

    def test_cpu_delegated_reads_subtree_control(self, monkeypatch, tmp_path):
        f = tmp_path / "cgroup.subtree_control"
        f.write_text("cpu memory pids\n")
        monkeypatch.setattr(resources, "_user_service_subtree_control", lambda: str(f))
        assert cpu_controller_delegated() is True

    def test_cpu_delegated_false_without_cpu(self, monkeypatch, tmp_path):
        f = tmp_path / "cgroup.subtree_control"
        f.write_text("memory pids\n")
        monkeypatch.setattr(resources, "_user_service_subtree_control", lambda: str(f))
        assert cpu_controller_delegated() is False

    def test_available_false_when_no_systemd_run(self, monkeypatch):
        monkeypatch.setattr(resources.shutil, "which", lambda _b: None)
        assert cgroup_limits_available() is False


@pytest.mark.skipif(not _HAS_CGROUP, reason="systemd-run / cgroup v2 unavailable")
class TestCgroupEndToEnd:
    """Real group-level enforcement — only where systemd-run + cgroup2 exist."""

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

    def test_memory_bomb_is_oom_killed(self, tmp_path):
        import os
        import shlex

        rt = SandboxRuntime(backend="none", harden_process=False, network="open", memory_max="64M")
        # Allocate ~512MB well over the 64M cap; the scope OOM-kills the tree.
        code = "b=bytearray(); [b.extend(bytearray(10*1024*1024)) for _ in range(50)]"
        cmd, env = _run(
            rt.wrap_command(
                f"python3 -c {shlex.quote(code)}",
                cwd=str(tmp_path),
                env=dict(os.environ),
            )
        )
        # The scope wrapper must be the outermost token.
        assert cmd.startswith("systemd-run --user --scope")
        rc, _, _ = self._exec(cmd, env)
        # OOM-kill -> 137 (128+SIGKILL) or Python's own MemoryError (rc=1).
        assert rc != 0

    def test_pids_max_caps_forks(self, tmp_path):
        import os
        import shlex

        rt = SandboxRuntime(backend="none", harden_process=False, network="open", pids_max=8)
        # Fork far beyond the cap; with TasksMax=8 most forks fail.
        code = (
            "import os,sys\n"
            "n=0\n"
            "for _ in range(200):\n"
            "    try:\n"
            "        pid=os.fork()\n"
            "    except OSError:\n"
            "        break\n"
            "    if pid==0:\n"
            "        import time; time.sleep(0.3); os._exit(0)\n"
            "    n+=1\n"
            "print('FORKED', n)\n"
        )
        cmd, env = _run(
            rt.wrap_command(
                f"python3 -c {shlex.quote(code)}",
                cwd=str(tmp_path),
                env=dict(os.environ),
            )
        )
        assert cmd.startswith("systemd-run --user --scope")
        rc, out, err = self._exec(cmd, env)
        # The fork count must be capped well below the 200 attempted.
        if "FORKED" in out:
            forked = int(out.split("FORKED")[1].split()[0])
            assert forked < 50, f"expected forks capped by TasksMax, got {forked}"


class TestRlimitFallbackEndToEnd:
    """Real per-process enforcement of the ``ulimit`` fallback.

    Forces the cgroup path unavailable (monkeypatched) so the runtime takes the
    rlimit seam, then runs a real payload that exceeds the ``ulimit -v`` address-
    space cap — Python raises ``MemoryError`` (rc != 0). Runs everywhere (no
    systemd needed): ``ulimit`` is a POSIX shell builtin.
    """

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

    def test_address_space_cap_blocks_big_alloc(self, monkeypatch, tmp_path):
        import os
        import shlex

        import mote.runtime.sandbox.runtime as rtmod

        monkeypatch.setattr(rtmod, "cgroup_limits_available", lambda: False)
        rt = SandboxRuntime(backend="none", harden_process=False, network="open", memory_max="128M")
        # Try to allocate ~512MB well over the 128M address-space cap.
        code = "b=bytearray(); [b.extend(bytearray(10*1024*1024)) for _ in range(50)]; print('OK')"
        cmd, env = _run(
            rt.wrap_command(
                f"python3 -c {shlex.quote(code)}",
                cwd=str(tmp_path),
                env=dict(os.environ),
            )
        )
        # The fallback path (no scope) set the cap in the shell.
        assert "systemd-run" not in cmd
        assert "ulimit -v" in cmd
        rc, out, err = self._exec(cmd, env)
        # The big allocation must fail (MemoryError / non-zero) — not print OK.
        assert rc != 0 or "OK" not in out
