#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the netns launcher (``network/orchestrator.py``).

Two layers:
  * pure helpers — config base64 round-trip, launcher argv/command shape,
    inner-argv wrapping, and the ``--info-fd``/``--seccomp`` flag patching that
    replaced the unreliable ``dup2`` dance. No root, no namespace.
  * an end-to-end sole-egress assertion behind a toolchain ``skipif``: a real
    netns whose only route out is the proxy, proving a raw socket to a public IP
    is dropped while the gateway:port is reachable.
"""
from __future__ import annotations

import base64
import json
import sys

import pytest
from mote.sandbox.network import enforce, orchestrator


class TestEncodeConfig:
    def test_round_trips_to_json(self):
        token = orchestrator.encode_config(
            bwrap_argv=["bwrap", "--info-fd", "3", "--", "true"],
            proxy_port=8080,
        )
        cfg = json.loads(base64.b64decode(token).decode("utf-8"))
        assert cfg["bwrap_argv"] == ["bwrap", "--info-fd", "3", "--", "true"]
        assert cfg["proxy_port"] == 8080

    def test_carries_seccomp_path_and_fd(self):
        token = orchestrator.encode_config(
            bwrap_argv=["bwrap"],
            proxy_port=1,
            seccomp_path="/tmp/sbx.bpf",
            seccomp_fd=9,
        )
        cfg = json.loads(base64.b64decode(token).decode("utf-8"))
        assert cfg["seccomp_path"] == "/tmp/sbx.bpf"
        assert cfg["seccomp_fd"] == 9

    def test_seccomp_defaults_to_none(self):
        token = orchestrator.encode_config(bwrap_argv=["bwrap"], proxy_port=1)
        cfg = json.loads(base64.b64decode(token).decode("utf-8"))
        assert cfg["seccomp_path"] is None
        assert cfg["seccomp_fd"] is None

    def test_token_is_argv_safe(self):
        # No whitespace / shell metacharacters — safe to pass bare on argv.
        token = orchestrator.encode_config(
            bwrap_argv=["bwrap", "--bind", "/a b/c", "--", "echo $HOME"],
            proxy_port=8080,
        )
        assert " " not in token
        assert "'" not in token and '"' not in token


class TestLauncherInvocation:
    def test_argv_runs_module_with_token(self):
        argv = orchestrator.launcher_argv("TOKEN")
        assert argv[0] == sys.executable
        assert argv[1] == "-m"
        assert argv[2] == "mote.sandbox.network.orchestrator"
        assert argv[-1] == "TOKEN"

    def test_command_is_shell_quoted_argv(self):
        cmd = orchestrator.launcher_command("TOKEN")
        # Equivalent to the argv, joined + quoted for create_subprocess_shell.
        assert cmd.split()[-1] == "TOKEN"
        assert "-m" in cmd
        assert "mote.sandbox.network.orchestrator" in cmd


class TestBuildInnerArgv:
    def test_wraps_payload_under_sh_c(self):
        argv = orchestrator.build_inner_argv("PRELUDE", ["/bin/echo", "hi"])
        assert argv[:3] == ["/bin/sh", "-c", "PRELUDE"]
        # $0 is "sbx"; the payload becomes $@.
        assert argv[3] == "sbx"
        assert argv[4:] == ["/bin/echo", "hi"]

    def test_empty_payload_still_well_formed(self):
        argv = orchestrator.build_inner_argv("PRELUDE", [])
        assert argv == ["/bin/sh", "-c", "PRELUDE", "sbx"]


class TestReplaceFlagValue:
    def test_replaces_token_after_flag(self):
        out = orchestrator._replace_flag_value(["bwrap", "--info-fd", "3", "--", "true"], "--info-fd", "7")
        assert out == ["bwrap", "--info-fd", "7", "--", "true"]

    def test_noop_when_flag_absent(self):
        argv = ["bwrap", "--", "true"]
        assert orchestrator._replace_flag_value(argv, "--seccomp", "9") == argv

    def test_does_not_mutate_input(self):
        argv = ["bwrap", "--info-fd", "3"]
        orchestrator._replace_flag_value(argv, "--info-fd", "7")
        assert argv == ["bwrap", "--info-fd", "3"]

    def test_flag_at_tail_is_safe(self):
        # A flag with no following token must not raise.
        out = orchestrator._replace_flag_value(["bwrap", "--info-fd"], "--info-fd", "7")
        assert out == ["bwrap", "--info-fd"]


class TestMainEntry:
    def test_missing_token_returns_2(self):
        assert orchestrator.main([]) == 2


@pytest.mark.skipif(
    not enforce.enforcement_available(),
    reason="netns toolchain (bwrap + slirp4netns + nft + /dev/net/tun) absent",
)
class TestSoleEgressEndToEnd:
    """The threat model: the proxy is the SOLE egress.

    Exercised through the real :class:`SandboxRuntime` API so the assertion
    covers the whole chain (bwrap --unshare-net + slirp gateway + nft lock),
    not just the builders. A raw socket to a public IP must be dropped even
    though the code never honours HTTP_PROXY.
    """

    def _run(self, code: str):
        import asyncio
        import os
        import shlex

        import mote
        from mote.sandbox.runtime import SandboxRuntime

        # The launcher runs ``python -m mote.sandbox.network.orchestrator``;
        # it needs the repo root on PYTHONPATH (in a deployed install mote is
        # already importable, but the test runs from source).
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(mote.__file__)))

        async def go():
            rt = SandboxRuntime(
                backend="bwrap",
                harden_process=False,
                seccomp=False,
                network="proxy",
                network_enforcement=True,
                allowed_domains=["example.com"],
            )
            await rt.start()
            if not rt._netns_egress:  # pragma: no cover - skipif should prevent
                await rt.shutdown()
                pytest.skip("netns egress did not engage on this host")
            cwd = os.getcwd()
            base = dict(os.environ)
            base["PYTHONPATH"] = repo_root + os.pathsep + base.get("PYTHONPATH", "")
            cmd, env = await rt.wrap_command(f"python3 -c {shlex.quote(code)}", cwd=cwd, env=base)
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            out, err = await proc.communicate()
            await rt.shutdown()
            return proc.returncode, out.decode(), err.decode()

        return asyncio.run(go())

    def test_raw_socket_to_public_ip_is_dropped(self):
        # A deliberate raw connect (NOT proxy-honouring) to a public IP must
        # time out / be refused — the nft default-drop is the backstop.
        code = (
            "import socket\n"
            "s=socket.socket(); s.settimeout(4)\n"
            "try:\n"
            " s.connect(('1.1.1.1',80)); print('CONNECTED')\n"
            "except OSError as e: print('BLOCKED', e.__class__.__name__)\n"
        )
        rc, out, err = self._run(code)
        assert rc == 0, err
        assert "BLOCKED" in out
        assert "CONNECTED" not in out

    def test_runs_as_userns_root(self):
        # --uid 0 --gid 0 must take effect (required for CAP_NET_ADMIN).
        code = "import os; print('UID', os.getuid())"
        rc, out, err = self._run(code)
        assert rc == 0, err
        assert "UID 0" in out

    def test_nft_lock_cannot_be_torn_down(self):
        # The payload inherits the netns as userns-root, but the prelude must
        # surrender CAP_NET_ADMIN before exec — so an attempt to flush the nft
        # lock fails (EPERM), and direct egress stays dropped afterwards. Without
        # the cap-drop the payload could `nft flush ruleset` and re-open the
        # still-live slirp NAT, collapsing P2 to P1's honour system.
        code = (
            "import subprocess, socket\n"
            # Try to tear down the firewall lock.
            "r = subprocess.run(['nft','flush','ruleset'],"
            " capture_output=True, text=True)\n"
            "print('FLUSH_RC', r.returncode)\n"
            # Whether or not the flush 'succeeded' from the shell's view, a raw
            # socket to a public IP must STILL be dropped.
            "s = socket.socket(); s.settimeout(4)\n"
            "try:\n"
            " s.connect(('1.1.1.1',80)); print('EGRESS CONNECTED')\n"
            "except OSError as e: print('EGRESS BLOCKED', e.__class__.__name__)\n"
        )
        rc, out, err = self._run(code)
        assert rc == 0, err
        # The flush must NOT succeed (non-zero rc from nft under dropped caps).
        assert "FLUSH_RC 0" not in out
        # And egress is still locked regardless.
        assert "EGRESS BLOCKED" in out
        assert "EGRESS CONNECTED" not in out

    def test_payload_is_unprivileged_after_drop(self):
        # Belt-and-braces: the payload's effective capability set is empty (the
        # prelude cleared it via `capsh --caps=""`). CapEff in /proc/self/status
        # must be all-zero.
        code = (
            "import re\n"
            "caps = open('/proc/self/status').read()\n"
            "m = re.search(r'CapEff:\\s*([0-9a-f]+)', caps)\n"
            "print('CAPEFF', m.group(1) if m else 'NONE')\n"
        )
        rc, out, err = self._run(code)
        assert rc == 0, err
        assert "CAPEFF 0000000000000000" in out
