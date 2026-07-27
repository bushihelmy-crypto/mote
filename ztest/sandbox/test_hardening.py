#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for process hardening (``sandbox.hardening``).

Covers the shell prelude shape, env-var stripping, and the preexec fallback's
effect on the child (verified by spawning a subprocess and inspecting
``/proc/self/status`` / its environment).
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from mote.runtime.sandbox.hardening import apply_in_child, harden_env, hardening_prelude


class TestHardeningPrelude:
    def test_disables_core_dumps(self):
        prelude = hardening_prelude()
        assert "ulimit -c 0" in prelude

    def test_unsets_ld_vars(self):
        prelude = hardening_prelude()
        assert "unset LD_PRELOAD" in prelude
        assert "unset LD_LIBRARY_PATH" in prelude
        assert "unset LD_AUDIT" in prelude

    def test_posix_sh_compatible(self):
        # No bashisms; statements joined with "; ".
        prelude = hardening_prelude()
        assert ";" in prelude
        assert "[[" not in prelude


class TestHardenEnv:
    def test_strips_ld_vars(self):
        env = {"PATH": "/bin", "LD_PRELOAD": "/evil.so", "LD_LIBRARY_PATH": "/x", "LD_AUDIT": "/y"}
        out = harden_env(env)
        assert "LD_PRELOAD" not in out
        assert "LD_LIBRARY_PATH" not in out
        assert "LD_AUDIT" not in out
        assert out["PATH"] == "/bin"

    def test_does_not_mutate_input(self):
        env = {"LD_PRELOAD": "/evil.so"}
        harden_env(env)
        assert env == {"LD_PRELOAD": "/evil.so"}

    def test_keeps_safe_vars(self):
        env = {"HOME": "/home/u", "USER": "u"}
        assert harden_env(env) == env


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux /proc only")
class TestApplyInChild:
    def test_child_is_non_dumpable(self):
        # Spawn a child that applies hardening then reports its rlimit. The
        # child inherits our import path via PYTHONPATH so ``mote.runtime.sandbox``
        # resolves regardless of cwd.
        code = (
            "from mote.runtime.sandbox.hardening import apply_in_child;"
            "apply_in_child();"
            "import resource;"
            "soft, hard = resource.getrlimit(resource.RLIMIT_CORE);"
            "print('CORE', soft, hard)"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(sys.path)
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        assert out.returncode == 0, out.stderr
        # RLIMIT_CORE soft limit driven to 0.
        assert "CORE 0" in out.stdout
