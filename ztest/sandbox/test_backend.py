#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for backend selection + bwrap argv + violation parsing.

The bwrap argv test does NOT require bwrap to be installed — ``build_argv`` is
pure string assembly. The end-to-end "writes are actually confined" test lives
in ``test_runtime.py`` behind a ``skipif`` guard.
"""
from __future__ import annotations

import os

import pytest

from metagpt.sandbox.backend import NullBackend, SandboxPolicy
from metagpt.sandbox.bwrap import BwrapBackend
from metagpt.sandbox.detect import detect_backend
from metagpt.sandbox.violations import SandboxViolation, parse_violations


class TestDetectBackend:
    def test_none_is_passthrough(self):
        assert detect_backend("none") == "none"

    def test_unknown_is_none(self):
        assert detect_backend("garbage") == "none"

    def test_auto_returns_known_kind(self):
        # On a host without bwrap -> "none"; with bwrap -> "bwrap". Either way a
        # known literal.
        assert detect_backend("auto") in ("bwrap", "none")


class TestNullBackend:
    def test_passthrough(self):
        backend = NullBackend()
        argv = backend.build_argv(SandboxPolicy(), ["/bin/sh", "-c", "echo hi"])
        assert argv == ["/bin/sh", "-c", "echo hi"]

    def test_always_available(self):
        assert NullBackend().available is True


class TestBwrapArgv:
    """Pure argv-assembly checks — no bwrap binary needed."""

    def test_baseline_flags(self, tmp_path):
        backend = BwrapBackend()
        policy = SandboxPolicy(writable_roots=[str(tmp_path)], cwd=str(tmp_path))
        argv = backend.build_argv(policy, ["/bin/sh", "-c", "echo hi"])
        # bwrap binary first, inner command last after the "--".
        assert argv[0].endswith("bwrap")
        assert "--die-with-parent" in argv
        assert "--unshare-user" in argv
        assert "--unshare-pid" in argv
        # Read-only root baseline.
        assert "--ro-bind" in argv
        i = argv.index("--ro-bind")
        assert argv[i + 1] == "/" and argv[i + 2] == "/"
        # Inner command is after the terminating "--".
        sep = argv.index("--")
        assert argv[sep + 1:] == ["/bin/sh", "-c", "echo hi"]

    def test_writable_root_bind(self, tmp_path):
        backend = BwrapBackend()
        policy = SandboxPolicy(writable_roots=[str(tmp_path)])
        argv = backend.build_argv(policy, ["true"])
        real = os.path.realpath(str(tmp_path))
        # --bind <real> <real> appears for the writable workspace.
        joined = " ".join(argv)
        assert f"--bind {real} {real}" in joined

    def test_readonly_override_repins(self, tmp_path):
        backend = BwrapBackend()
        cfg = tmp_path / "config.yaml"
        cfg.write_text("k: v")
        policy = SandboxPolicy(
            writable_roots=[str(tmp_path)],
            readonly_overrides=[str(cfg)],
        )
        argv = backend.build_argv(policy, ["true"])
        real = os.path.realpath(str(cfg))
        joined = " ".join(argv)
        # The config is re-pinned read-only AFTER the writable bind.
        assert f"--ro-bind {real} {real}" in joined

    def test_missing_writable_root_skipped(self, tmp_path):
        backend = BwrapBackend()
        missing = str(tmp_path / "does-not-exist")
        policy = SandboxPolicy(writable_roots=[missing])
        argv = backend.build_argv(policy, ["true"])
        assert os.path.realpath(missing) not in argv

    def test_unshare_net_optional(self, tmp_path):
        backend = BwrapBackend()
        off = BwrapBackend().build_argv(SandboxPolicy(writable_roots=[str(tmp_path)]), ["true"])
        on = BwrapBackend().build_argv(
            SandboxPolicy(writable_roots=[str(tmp_path)], unshare_net=True), ["true"]
        )
        assert "--unshare-net" not in off
        assert "--unshare-net" in on

    def test_tmpfs_tmp(self, tmp_path):
        argv = BwrapBackend().build_argv(SandboxPolicy(writable_roots=[str(tmp_path)]), ["true"])
        joined = " ".join(argv)
        assert "--tmpfs /tmp" in joined

    def test_tmpfs_precedes_writable_binds(self, tmp_path):
        # --tmpfs /tmp must come BEFORE the --bind for a writable root so a root
        # living under /tmp is not masked by the fresh tmpfs.
        argv = BwrapBackend().build_argv(SandboxPolicy(writable_roots=[str(tmp_path)]), ["true"])
        real = os.path.realpath(str(tmp_path))
        tmpfs_i = argv.index("--tmpfs")
        bind_i = argv.index(real)  # the --bind source position
        assert tmpfs_i < bind_i

    def test_dev_dev_after_ro_root(self, tmp_path):
        # The read-only root baseline must precede --dev so the fresh writable
        # /dev (with /dev/null) is not re-covered read-only.
        argv = BwrapBackend().build_argv(SandboxPolicy(writable_roots=[str(tmp_path)]), ["true"])
        ro_root = argv.index("--ro-bind")  # first --ro-bind is the / / baseline
        dev_i = argv.index("--dev")
        assert ro_root < dev_i

    def test_seccomp_fd_emitted(self, tmp_path):
        argv = BwrapBackend().build_argv(
            SandboxPolicy(writable_roots=[str(tmp_path)], seccomp_fd=9), ["true"]
        )
        assert "--seccomp" in argv
        assert argv[argv.index("--seccomp") + 1] == "9"

    def test_seccomp_fd_absent_when_none(self, tmp_path):
        argv = BwrapBackend().build_argv(SandboxPolicy(writable_roots=[str(tmp_path)]), ["true"])
        assert "--seccomp" not in argv

    def test_cap_net_admin_optional(self, tmp_path):
        off = BwrapBackend().build_argv(SandboxPolicy(writable_roots=[str(tmp_path)]), ["true"])
        on = BwrapBackend().build_argv(
            SandboxPolicy(writable_roots=[str(tmp_path)], cap_net_admin=True), ["true"]
        )
        assert "--cap-add" not in off
        joined_on = " ".join(on)
        assert "--cap-add CAP_NET_ADMIN" in joined_on

    def test_dev_binds_emitted_for_existing(self, tmp_path):
        # /dev/null always exists; a bogus path is skipped.
        argv = BwrapBackend().build_argv(
            SandboxPolicy(
                writable_roots=[str(tmp_path)],
                dev_binds=["/dev/null", "/dev/does-not-exist-xyz"],
            ),
            ["true"],
        )
        joined = " ".join(argv)
        assert "--dev-bind /dev/null /dev/null" in joined
        assert "/dev/does-not-exist-xyz" not in argv

    def test_uid_root_optional(self, tmp_path):
        off = BwrapBackend().build_argv(SandboxPolicy(writable_roots=[str(tmp_path)]), ["true"])
        on = BwrapBackend().build_argv(
            SandboxPolicy(writable_roots=[str(tmp_path)], uid_root=True), ["true"]
        )
        assert "--uid" not in off
        joined_on = " ".join(on)
        assert "--uid 0 --gid 0" in joined_on

    def test_uid_root_precedes_cap_add(self, tmp_path):
        # --uid 0 --gid 0 must come BEFORE --cap-add: without the userns-root
        # mapping bwrap silently drops CAP_NET_ADMIN.
        argv = BwrapBackend().build_argv(
            SandboxPolicy(writable_roots=[str(tmp_path)], uid_root=True, cap_net_admin=True),
            ["true"],
        )
        assert argv.index("--uid") < argv.index("--cap-add")

    def test_info_fd_emitted(self, tmp_path):
        argv = BwrapBackend().build_argv(
            SandboxPolicy(writable_roots=[str(tmp_path)], info_fd=3), ["true"]
        )
        assert "--info-fd" in argv
        assert argv[argv.index("--info-fd") + 1] == "3"

    def test_info_fd_absent_when_none(self, tmp_path):
        argv = BwrapBackend().build_argv(SandboxPolicy(writable_roots=[str(tmp_path)]), ["true"])
        assert "--info-fd" not in argv

    def test_extra_writable_bind(self, tmp_path):
        extra = tmp_path / "scratch"
        extra.mkdir()
        argv = BwrapBackend().build_argv(
            SandboxPolicy(writable_roots=[str(tmp_path)], extra_writable=[str(extra)]),
            ["true"],
        )
        real = os.path.realpath(str(extra))
        assert f"--bind {real} {real}" in " ".join(argv)

    def test_extra_writable_missing_skipped(self, tmp_path):
        missing = str(tmp_path / "nope")
        argv = BwrapBackend().build_argv(
            SandboxPolicy(writable_roots=[str(tmp_path)], extra_writable=[missing]),
            ["true"],
        )
        assert os.path.realpath(missing) not in argv


class TestParseViolations:
    def test_no_stderr_returns_empty(self):
        assert parse_violations("") == []

    def test_clean_stderr_returns_empty(self):
        assert parse_violations("hello\nworld\n") == []

    def test_eperm_phrase_detected(self):
        out = parse_violations("touch: cannot touch '/etc/x': Read-only file system")
        assert len(out) == 1
        assert out[0].kind == "fs"
        assert "Read-only file system" in out[0].detail

    def test_permission_denied_detected(self):
        out = parse_violations("sh: /etc/x: Permission denied")
        assert len(out) == 1
        assert out[0].kind == "fs"

    def test_bwrap_setup_failure(self):
        out = parse_violations("bwrap: Creating new namespace failed: Operation not permitted")
        assert any(v.kind == "setup" for v in out)

    def test_violation_render(self):
        v = SandboxViolation(kind="fs", message="blocked", detail="Read-only file system")
        assert v.render() == "[sandbox:fs] blocked (Read-only file system)"
