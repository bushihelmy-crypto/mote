#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the SandboxRuntime façade (``sandbox.runtime``).

Two layers:
  * pure wrapping/degradation logic (no bwrap needed) — wrap_command shape,
    hardening prelude injection, network env, fail_if_unavailable;
  * end-to-end filesystem confinement behind ``skipif(bwrap missing)``.

All async exercised via ``asyncio.run`` (no pytest-asyncio).
"""
from __future__ import annotations

import asyncio
import os
import shutil

import pytest

from metagpt.sandbox.backend import SandboxPolicy
from metagpt.sandbox.runtime import SandboxRuntime

_HAS_BWRAP = shutil.which("bwrap") is not None and os.name == "posix"


def _run(coro):
    return asyncio.run(coro)


class TestWrapCommandNullBackend:
    def test_passthrough_when_no_harden_no_backend(self):
        rt = SandboxRuntime(backend="none", harden_process=False, network="open")
        cmd, env = _run(rt.wrap_command("echo hi", env={"PATH": "/bin"}))
        # True passthrough — command untouched.
        assert cmd == "echo hi"

    def test_hardening_wraps_in_sh(self):
        rt = SandboxRuntime(backend="none", harden_process=True, network="open")
        cmd, env = _run(rt.wrap_command("echo hi", env={"PATH": "/bin"}))
        assert "/bin/sh" in cmd
        assert "-c" in cmd
        assert "ulimit -c 0" in cmd
        assert "echo hi" in cmd

    def test_ld_preload_stripped_from_env(self):
        rt = SandboxRuntime(backend="none", harden_process=True, network="open")
        _, env = _run(rt.wrap_command("echo hi", env={"PATH": "/bin", "LD_PRELOAD": "/evil.so"}))
        assert "LD_PRELOAD" not in env


class TestNetworkEnv:
    def test_proxy_injects_proxy_vars(self):
        rt = SandboxRuntime(backend="none", harden_process=False, network="proxy", allowed_domains=["x.com"])
        _, env = _run(rt.wrap_command("echo hi", env={"PATH": "/bin"}))
        assert env["HTTP_PROXY"].startswith("http://127.0.0.1:")
        assert env["HTTPS_PROXY"] == env["HTTP_PROXY"]
        assert "NO_PROXY" in env
        _run(rt.shutdown())

    def test_off_points_at_dead_port(self):
        rt = SandboxRuntime(backend="none", harden_process=False, network="off")
        _, env = _run(rt.wrap_command("echo hi", env={"PATH": "/bin"}))
        assert env["HTTP_PROXY"] == "http://127.0.0.1:9"

    def test_open_leaves_env_alone(self):
        rt = SandboxRuntime(backend="none", harden_process=False, network="open")
        _, env = _run(rt.wrap_command("echo hi", env={"PATH": "/bin"}))
        assert "HTTP_PROXY" not in env


class TestDegradation:
    def test_unavailable_backend_warns_when_soft(self):
        # Force a bwrap backend on a host that may lack it; soft fail -> NullBackend.
        rt = SandboxRuntime(backend="bwrap", fail_if_unavailable=False, harden_process=False, network="open")
        cmd, _ = _run(rt.wrap_command("echo hi", env={"PATH": "/bin"}))
        # Whether bwrap is present or not, soft-fail never raises.
        assert "echo hi" in cmd

    @pytest.mark.skipif(_HAS_BWRAP, reason="needs a host WITHOUT bwrap to test hard-fail")
    def test_unavailable_backend_raises_when_hard(self):
        rt = SandboxRuntime(backend="bwrap", fail_if_unavailable=True, network="open")
        with pytest.raises(RuntimeError):
            _run(rt.wrap_command("echo hi", env={"PATH": "/bin"}))


class TestPolicyProvider:
    def test_cwd_added_to_writable_roots(self, tmp_path):
        captured = {}

        def provider():
            return SandboxPolicy(writable_roots=["/already"])

        rt = SandboxRuntime(backend="none", harden_process=False, network="open", policy_provider=provider)
        # _policy_for is internal but the cwd-merge behaviour is the contract.
        policy = rt._policy_for(str(tmp_path))
        assert str(tmp_path) in policy.writable_roots
        assert "/already" in policy.writable_roots

    def test_off_sets_unshare_net(self, tmp_path):
        rt = SandboxRuntime(backend="none", harden_process=False, network="off")
        policy = rt._policy_for(str(tmp_path))
        assert policy.unshare_net is True

    def test_proxy_does_not_unshare_net(self, tmp_path):
        rt = SandboxRuntime(backend="none", harden_process=False, network="open")
        policy = rt._policy_for(str(tmp_path))
        assert policy.unshare_net is False

    def test_extra_writable_merged_into_policy(self, tmp_path):
        # extra_writable paths are appended to the policy's extra_writable so the
        # backend emits a --bind per path (the kernel ipc:// socket dir seam).
        rt = SandboxRuntime(backend="none", harden_process=False, network="open")
        policy = rt._policy_for(str(tmp_path), extra_writable=["/scratch/sock"])
        assert "/scratch/sock" in policy.extra_writable


class TestWrapExecExtraWritable:
    """``wrap_exec(extra_writable=...)`` threads through to a bwrap ``--bind``.

    Uses a fake backend that records the policy it was handed so the contract is
    exercised without needing bwrap; an end-to-end variant runs behind skipif.
    """

    def test_extra_writable_reaches_backend(self, tmp_path):
        captured = {}

        class _RecordingBackend:
            name = "rec"

            def build_argv(self, policy, inner):
                captured["policy"] = policy
                return list(inner)

        rt = SandboxRuntime(backend="none", harden_process=False, network="open")
        # start() would reset _backend; mark started so our recording backend
        # survives (we only need the wrapping contract, not a live sandbox).
        rt._started = True
        rt._backend = _RecordingBackend()
        d = str(tmp_path / "sock")
        _run(rt.wrap_exec(["/bin/true"], cwd=str(tmp_path), env={"PATH": "/bin"}, extra_writable=[d]))
        assert d in captured["policy"].extra_writable

    @pytest.mark.skipif(not _HAS_BWRAP, reason="bwrap not installed")
    def test_extra_writable_emits_bind_in_bwrap_argv(self, tmp_path):
        # The extra dir must exist for bwrap to bind it (build_argv skips missing).
        sock = tmp_path / "sock"
        sock.mkdir()
        rt = SandboxRuntime(backend="bwrap", harden_process=False, network="open")
        argv, _ = _run(
            rt.wrap_exec(
                ["/bin/true"], cwd=str(tmp_path), env={"PATH": "/bin"},
                extra_writable=[str(sock)],
            )
        )
        joined = " ".join(argv)
        assert f"--bind {sock} {sock}" in joined


class TestNetnsEgressPolicy:
    """Policy mutation + wrapping under the netns sole-egress chain.

    These don't need the slirp/nft toolchain: ``_netns_egress`` is forced on +
    a proxy stub is injected, so the policy/argv contract is exercised purely.
    """

    class _FakeProxy:
        port = 44399
        url = "http://127.0.0.1:44399"

        async def shutdown(self):  # pragma: no cover - trivial
            pass

    def _runtime(self):
        rt = SandboxRuntime(backend="none", harden_process=False, network="proxy")
        rt._netns_egress = True
        rt._proxy = self._FakeProxy()
        return rt

    def test_policy_sets_netns_flags(self, tmp_path):
        from metagpt.sandbox.network.enforce import TUN_DEVICE

        rt = self._runtime()
        policy = rt._policy_for(str(tmp_path))
        assert policy.unshare_net is True
        assert policy.uid_root is True
        assert policy.cap_net_admin is True
        assert TUN_DEVICE in policy.dev_binds
        assert policy.info_fd == 3

    def test_proxy_env_points_at_gateway_not_loopback(self):
        rt = self._runtime()
        env = rt._apply_network_env({"PATH": "/bin"})
        # Inside the netns the host loopback is unreachable — must use the
        # slirp gateway URL.
        assert env["HTTP_PROXY"] == "http://10.0.2.2:44399"
        assert "127.0.0.1" not in env["HTTP_PROXY"]

    def test_wrap_command_emits_launcher(self, tmp_path):
        rt = self._runtime()
        # NullBackend can't build a real netns argv but the launcher path still
        # assembles a token; force a usable backend to assemble the bwrap argv.
        from metagpt.sandbox.bwrap import BwrapBackend

        rt._backend = BwrapBackend()
        cmd, _ = _run(rt.wrap_command("echo hi", cwd=str(tmp_path), env={"PATH": "/bin"}))
        assert "metagpt.sandbox.network.orchestrator" in cmd

    def test_wrap_exec_emits_launcher(self, tmp_path):
        rt = self._runtime()
        from metagpt.sandbox.bwrap import BwrapBackend

        rt._backend = BwrapBackend()
        argv, _ = _run(rt.wrap_exec(["/bin/bash", "-i"], cwd=str(tmp_path), env={"PATH": "/bin"}))
        assert "-m" in argv
        assert "metagpt.sandbox.network.orchestrator" in argv

    def test_wrap_exec_extra_writable_in_netns_bwrap_argv(self, tmp_path):
        # extra_writable must survive into the bwrap argv encoded inside the
        # netns launcher's config token (the kernel ipc:// socket dir seam under
        # sole-egress). Decode the base64 token and assert the --bind is present.
        import base64
        import json

        sock = tmp_path / "sock"
        sock.mkdir()
        rt = self._runtime()
        from metagpt.sandbox.bwrap import BwrapBackend

        # Mark started so wrap_exec's start() doesn't reset our backend (the fake
        # proxy is already wired by _runtime()).
        rt._started = True
        rt._backend = BwrapBackend()
        argv, _ = _run(
            rt.wrap_exec(
                ["/bin/bash", "-i"], cwd=str(tmp_path), env={"PATH": "/bin"},
                extra_writable=[str(sock)],
            )
        )
        token = argv[-1]
        payload = json.loads(base64.b64decode(token))
        bwrap_argv = payload["bwrap_argv"]
        joined = " ".join(bwrap_argv)
        assert f"--bind {sock} {sock}" in joined

    def test_launcher_build_failure_falls_back(self, tmp_path):
        # A broken backend (build_argv raises) must not break the command path —
        # the netns launcher build is best-effort, falling back to direct bwrap.
        rt = self._runtime()

        class _BrokenBackend:
            name = "broken"

            def build_argv(self, policy, inner):
                raise RuntimeError("boom")

        rt._backend = _BrokenBackend()
        assert rt._build_netns_launcher_command(["true"], str(tmp_path)) is None


class TestCgroupLimitsWrapping:
    """Resource-limit (cgroup) prefix prepending — no systemd needed.

    The host probes are monkeypatched so the runtime believes cgroup limits are
    available; we assert the ``systemd-run --user --scope`` prefix is prepended
    as the outermost wrapper of both seams, that it degrades to a no-op when
    unavailable / empty, and that the seccomp ``9<path`` redirect stays last.
    """

    def _patch_available(self, monkeypatch, *, available=True, cpu=False):
        import metagpt.sandbox.runtime as rtmod

        monkeypatch.setattr(rtmod, "cgroup_limits_available", lambda: available)
        monkeypatch.setattr(rtmod, "cpu_controller_delegated", lambda: cpu)

    def test_wrap_command_prepends_scope(self, monkeypatch, tmp_path):
        self._patch_available(monkeypatch)
        rt = SandboxRuntime(
            backend="none", harden_process=True, network="open", memory_max="64M", pids_max=8
        )
        cmd, _ = _run(rt.wrap_command("echo hi", cwd=str(tmp_path), env={"PATH": "/bin"}))
        assert cmd.startswith("systemd-run --user --scope")
        assert "MemoryMax=64M" in cmd
        assert "TasksMax=8" in cmd
        # The inner hardened command still runs.
        assert "echo hi" in cmd

    def test_wrap_command_passthrough_scoped_via_sh(self, monkeypatch, tmp_path):
        # null backend + no harden + a cgroup cap: the raw command must be routed
        # through `sh -c` so shell operators stay inside the scope.
        self._patch_available(monkeypatch)
        rt = SandboxRuntime(backend="none", harden_process=False, network="open", memory_max="64M")
        cmd, _ = _run(rt.wrap_command("echo a && echo b", cwd=str(tmp_path), env={"PATH": "/bin"}))
        assert cmd.startswith("systemd-run --user --scope")
        assert "/bin/sh" in cmd
        assert "echo a && echo b" in cmd

    def test_wrap_exec_prepends_scope(self, monkeypatch, tmp_path):
        self._patch_available(monkeypatch)
        rt = SandboxRuntime(backend="none", harden_process=False, network="open", pids_max=8)
        argv, _ = _run(rt.wrap_exec(["/bin/bash", "-i"], cwd=str(tmp_path), env={"PATH": "/bin"}))
        assert argv[:4] == ["systemd-run", "--user", "--scope", "--quiet"]
        assert "TasksMax=8" in argv
        # The original argv survives at the tail.
        assert argv[-2:] == ["/bin/bash", "-i"]

    def test_no_prefix_when_limits_empty(self, monkeypatch, tmp_path):
        self._patch_available(monkeypatch)
        rt = SandboxRuntime(backend="none", harden_process=True, network="open")
        cmd, _ = _run(rt.wrap_command("echo hi", cwd=str(tmp_path), env={"PATH": "/bin"}))
        assert "systemd-run" not in cmd

    def test_no_prefix_when_unavailable(self, monkeypatch, tmp_path):
        # Limits requested but the host can't apply them -> degrade to no-op.
        self._patch_available(monkeypatch, available=False)
        rt = SandboxRuntime(backend="none", harden_process=True, network="open", memory_max="64M")
        cmd, _ = _run(rt.wrap_command("echo hi", cwd=str(tmp_path), env={"PATH": "/bin"}))
        assert "systemd-run" not in cmd

    def test_limits_provider_read_fresh_per_wrap(self, monkeypatch, tmp_path):
        # The dynamic contract: a limits_provider is read on every wrap, so a
        # session-time cap change is honoured on the next command without
        # rebuilding the runtime (mirrors the policy_provider seam).
        from metagpt.sandbox.resources import ResourceLimits

        self._patch_available(monkeypatch)
        live = ResourceLimits(memory_max="64M")
        rt = SandboxRuntime(
            backend="none", harden_process=True, network="open",
            limits_provider=lambda: live,
        )
        cmd, _ = _run(rt.wrap_command("echo hi", cwd=str(tmp_path), env={"PATH": "/bin"}))
        assert "MemoryMax=64M" in cmd
        # Adjust the live limits; the next wrap reflects it (no rebuild).
        live.memory_max = "128M"
        cmd2, _ = _run(rt.wrap_command("echo hi", cwd=str(tmp_path), env={"PATH": "/bin"}))
        assert "MemoryMax=128M" in cmd2
        assert "MemoryMax=64M" not in cmd2

    def test_provider_overrides_static_kwargs(self, monkeypatch, tmp_path):
        # When both a provider and static kwargs are present the provider wins
        # (the static caps are only the no-provider fallback baseline).
        from metagpt.sandbox.resources import ResourceLimits

        self._patch_available(monkeypatch)
        rt = SandboxRuntime(
            backend="none", harden_process=True, network="open",
            memory_max="999M",  # static baseline, should be ignored
            limits_provider=lambda: ResourceLimits(pids_max=8),
        )
        cmd, _ = _run(rt.wrap_command("echo hi", cwd=str(tmp_path), env={"PATH": "/bin"}))
        assert "TasksMax=8" in cmd
        assert "999M" not in cmd

    def test_cpu_quota_dropped_when_not_delegated(self, monkeypatch, tmp_path):
        self._patch_available(monkeypatch, available=True, cpu=False)
        rt = SandboxRuntime(
            backend="none", harden_process=False, network="open",
            memory_max="64M", cpu_quota="200%",
        )
        cmd, _ = _run(rt.wrap_command("echo hi", cwd=str(tmp_path), env={"PATH": "/bin"}))
        assert "CPUQuota" not in cmd
        assert "MemoryMax=64M" in cmd

    def test_cpu_quota_emitted_when_delegated(self, monkeypatch, tmp_path):
        self._patch_available(monkeypatch, available=True, cpu=True)
        rt = SandboxRuntime(
            backend="none", harden_process=False, network="open", cpu_quota="200%"
        )
        cmd, _ = _run(rt.wrap_command("echo hi", cwd=str(tmp_path), env={"PATH": "/bin"}))
        assert "CPUQuota=200%" in cmd

    @pytest.mark.skipif(not _HAS_BWRAP, reason="bwrap not installed")
    def test_seccomp_redirect_stays_last_with_scope(self, monkeypatch, tmp_path):
        # The cgroup prefix is outermost but the BPF `9<path` redirect must
        # remain the very last token of the command string.
        self._patch_available(monkeypatch)
        rt = SandboxRuntime(
            backend="bwrap", harden_process=False, network="open",
            seccomp=True, memory_max="64M",
        )
        cmd, _ = _run(rt.wrap_command("echo hi", cwd=str(tmp_path), env={"PATH": "/bin"}))
        assert cmd.startswith("systemd-run --user --scope")
        # If a seccomp filter was built, the redirect is appended last.
        if rt._seccomp_bpf_path is not None:
            assert "9<" in cmd
            assert cmd.rstrip().endswith(rt._seccomp_bpf_path) or cmd.rstrip().endswith(
                f"'{rt._seccomp_bpf_path}'"
            )
        _run(rt.shutdown())

    def test_netns_launcher_gets_scope(self, monkeypatch, tmp_path):
        # The netns launcher path (forced on) must also be wrapped in the scope.
        self._patch_available(monkeypatch)

        class _FakeProxy:
            port = 44399
            url = "http://127.0.0.1:44399"

            async def shutdown(self):  # pragma: no cover - trivial
                pass

        from metagpt.sandbox.bwrap import BwrapBackend

        rt = SandboxRuntime(backend="none", harden_process=False, network="proxy", memory_max="64M")
        rt._netns_egress = True
        rt._proxy = _FakeProxy()
        rt._backend = BwrapBackend()
        cmd, _ = _run(rt.wrap_command("echo hi", cwd=str(tmp_path), env={"PATH": "/bin"}))
        assert cmd.startswith("systemd-run --user --scope")
        assert "metagpt.sandbox.network.orchestrator" in cmd


class TestRlimitFallbackWrapping:
    """Per-process ``ulimit`` fallback wiring — engages ONLY when the cgroup
    scope is unavailable (the two are mutually exclusive: the scope already caps
    the whole tree). The host probes are monkeypatched so the runtime believes
    cgroup limits are unavailable, exercising the fallback seam without needing a
    systemd-less host.
    """

    def _patch_unavailable(self, monkeypatch):
        import metagpt.sandbox.runtime as rtmod

        monkeypatch.setattr(rtmod, "cgroup_limits_available", lambda: False)

    def test_wrap_command_injects_ulimit_when_cgroup_down(self, monkeypatch, tmp_path):
        self._patch_unavailable(monkeypatch)
        rt = SandboxRuntime(
            backend="none", harden_process=False, network="open",
            memory_max="64M", pids_max=8,
        )
        cmd, _ = _run(rt.wrap_command("echo hi", cwd=str(tmp_path), env={"PATH": "/bin"}))
        # No scope (cgroup down) but the per-process caps are set in the shell.
        assert "systemd-run" not in cmd
        assert "ulimit -v 65536" in cmd
        assert "ulimit -u 8" in cmd
        assert "echo hi" in cmd

    def test_wrap_exec_injects_ulimit_via_shim(self, monkeypatch, tmp_path):
        self._patch_unavailable(monkeypatch)
        rt = SandboxRuntime(
            backend="none", harden_process=False, network="open", pids_max=8
        )
        argv, _ = _run(rt.wrap_exec(["/bin/bash", "-i"], cwd=str(tmp_path), env={"PATH": "/bin"}))
        # The argv path wraps in an ``sh -c '… exec "$@"'`` shim so the exec'd
        # shell + descendants inherit the cap; the original argv survives.
        assert argv[0] == "/bin/sh"
        joined = " ".join(argv)
        assert "ulimit -u 8" in joined
        assert 'exec "$@"' in joined
        assert argv[-2:] == ["/bin/bash", "-i"]

    def test_no_ulimit_when_cgroup_available(self, monkeypatch, tmp_path):
        # The cgroup scope is mutually exclusive with the rlimit fallback: when
        # the scope is active the tree is already capped, so no ulimit is added.
        import metagpt.sandbox.runtime as rtmod

        monkeypatch.setattr(rtmod, "cgroup_limits_available", lambda: True)
        monkeypatch.setattr(rtmod, "cpu_controller_delegated", lambda: False)
        rt = SandboxRuntime(
            backend="none", harden_process=False, network="open", memory_max="64M"
        )
        cmd, _ = _run(rt.wrap_command("echo hi", cwd=str(tmp_path), env={"PATH": "/bin"}))
        assert cmd.startswith("systemd-run --user --scope")
        assert "ulimit" not in cmd

    def test_no_ulimit_when_limits_empty(self, monkeypatch, tmp_path):
        self._patch_unavailable(monkeypatch)
        rt = SandboxRuntime(backend="none", harden_process=False, network="open")
        cmd, _ = _run(rt.wrap_command("echo hi", cwd=str(tmp_path), env={"PATH": "/bin"}))
        # No limits requested -> true passthrough, no shell wrap.
        assert cmd == "echo hi"

    def test_ulimit_coexists_with_hardening(self, monkeypatch, tmp_path):
        # Both gates fire independently: the hardening prelude AND the rlimit
        # fallback share the one ``sh -c`` body.
        self._patch_unavailable(monkeypatch)
        rt = SandboxRuntime(
            backend="none", harden_process=True, network="open", pids_max=8
        )
        cmd, _ = _run(rt.wrap_command("echo hi", cwd=str(tmp_path), env={"PATH": "/bin"}))
        assert "ulimit -c 0" in cmd  # hardening
        assert "ulimit -u 8" in cmd  # rlimit fallback
        assert "echo hi" in cmd

    def test_fallback_reads_live_limits(self, monkeypatch, tmp_path):
        # Dynamic: a limits_provider is read per wrap, so a session cap change is
        # honoured on the next command (mirrors the cgroup path's contract).
        from metagpt.sandbox.resources import ResourceLimits

        self._patch_unavailable(monkeypatch)
        live = ResourceLimits(pids_max=8)
        rt = SandboxRuntime(
            backend="none", harden_process=False, network="open",
            limits_provider=lambda: live,
        )
        cmd, _ = _run(rt.wrap_command("echo hi", cwd=str(tmp_path), env={"PATH": "/bin"}))
        assert "ulimit -u 8" in cmd
        live.pids_max = 16
        cmd2, _ = _run(rt.wrap_command("echo hi", cwd=str(tmp_path), env={"PATH": "/bin"}))
        assert "ulimit -u 16" in cmd2
        assert "ulimit -u 8" not in cmd2


@pytest.mark.skipif(not _HAS_BWRAP, reason="bwrap not installed")
class TestBwrapEndToEnd:
    """Real confinement — only runs where bwrap is available."""

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

    def test_write_inside_cwd_succeeds(self, tmp_path):
        rt = SandboxRuntime(backend="bwrap", harden_process=True, network="open")
        target = tmp_path / "ok.txt"
        cmd, env = _run(rt.wrap_command(f"echo hi > {target}", cwd=str(tmp_path), env=dict(os.environ)))
        rc, _, _ = self._exec(cmd, env)
        assert rc == 0
        assert target.exists()

    def test_write_outside_root_blocked(self, tmp_path):
        rt = SandboxRuntime(backend="bwrap", harden_process=True, network="open")
        cmd, env = _run(rt.wrap_command("echo x > /etc/sandbox_probe", cwd=str(tmp_path), env=dict(os.environ)))
        rc, _, err = self._exec(cmd, env)
        assert rc != 0
        assert not os.path.exists("/etc/sandbox_probe")

    def test_root_is_readonly(self, tmp_path):
        rt = SandboxRuntime(backend="bwrap", harden_process=True, network="open")
        cmd, env = _run(rt.wrap_command("touch /usr/sandbox_probe", cwd=str(tmp_path), env=dict(os.environ)))
        rc, _, _ = self._exec(cmd, env)
        assert rc != 0

    def test_no_dev_null_error_on_stderr(self, tmp_path):
        # Regression: --ro-bind / / must precede --dev /dev, else /dev is re-covered
        # read-only and every command leaks "cannot create /dev/null" to stderr.
        rt = SandboxRuntime(backend="bwrap", harden_process=True, network="open")
        cmd, env = _run(rt.wrap_command("echo ok > /dev/null && echo done", cwd=str(tmp_path), env=dict(os.environ)))
        rc, out, err = self._exec(cmd, env)
        assert rc == 0
        assert "done" in out
        assert "/dev/null" not in err

    def test_network_off_blocks_external_egress(self, tmp_path):
        # network="off" => --unshare-net => external connect dies with ENETUNREACH
        # (errno 101) while loopback survives (Jupyter ZMQ keeps working).
        rt = SandboxRuntime(backend="bwrap", harden_process=False, network="off")
        code = (
            "import socket,errno\n"
            "s=socket.socket(); s.settimeout(2)\n"
            "try:\n"
            " s.connect(('1.1.1.1',80)); print('CONNECTED')\n"
            "except OSError as e: print('errno', e.errno)\n"
        )
        import shlex

        cmd, env = _run(
            rt.wrap_command(f"python3 -c {shlex.quote(code)}", cwd=str(tmp_path), env=dict(os.environ))
        )
        rc, out, err = self._exec(cmd, env)
        _run(rt.shutdown())
        assert rc == 0, err
        assert "errno 101" in out  # ENETUNREACH
