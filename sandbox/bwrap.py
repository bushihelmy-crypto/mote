"""Bubblewrap backend — translate a :class:`SandboxPolicy` into ``bwrap`` argv.

bwrap (bubblewrap) builds an unprivileged user namespace with a custom mount
tree, then ``exec``s the inner command inside it. Our confinement recipe:

  * ``--die-with-parent`` — kill the sandbox if we die (no orphans).
  * ``--unshare-user --unshare-pid`` — fresh user + pid namespaces (the
    sandboxed process can't see/signal host pids; pid 1 is the inner command).
  * ``--proc /proc --dev /dev`` — minimal /proc + /dev so normal programs work.
  * ``--ro-bind / /`` — the entire host root, READ-ONLY, as the baseline.
  * ``--bind <root> <root>`` per writable root — punch read-write holes for the
    workspace (+ session-granted dirs).
  * ``--ro-bind <p> <p>`` per readonly override — re-pin sensitive paths
    (``.git`` / ``config.yaml`` / ``.agent_sessions``) read-only even though
    they sit inside a writable root (anti-escape; mirrors Codex/CC).
  * ``--tmpfs /tmp`` — a private, writable scratch /tmp.
  * ``--unshare-net`` — only when the policy asks (P1 default leaves net shared
    and relies on proxy env injection; see ``network/netns.py``).

Version compatibility: we wrap the inner command with ``-- /bin/sh -c`` rather
than relying on ``--argv0`` (added in bwrap 0.5.0), so the wrapper runs on older
builds without a version probe.
"""
from __future__ import annotations

import os

from mote.sandbox.backend import SandboxBackend, SandboxPolicy
from mote.sandbox.detect import bwrap_available, bwrap_path


class BwrapBackend(SandboxBackend):
    """Confine commands with bubblewrap user+mount namespaces."""

    name = "bwrap"

    @property
    def available(self) -> bool:
        return bwrap_available()

    def build_argv(self, policy: SandboxPolicy, inner_argv: list[str]) -> list[str]:
        """Return ``[bwrap, <flags...>, --, *inner_argv]``.

        ``inner_argv`` is already the fully-formed inner command (e.g.
        ``["/bin/sh", "-c", "<prelude>; <cmd>"]``); we only prepend the sandbox.
        """
        bwrap = bwrap_path() or "bwrap"
        argv: list[str] = [bwrap]

        # Lifecycle + namespaces. NOTE: the read-only root baseline is bound
        # FIRST, then --proc/--dev layer their fresh (writable) mounts on top.
        # Order matters: bwrap applies mounts left-to-right, so binding
        # ``--ro-bind / /`` AFTER ``--dev /dev`` would re-cover /dev read-only
        # and break writes to /dev/null, /dev/urandom, etc.
        argv += [
            "--die-with-parent",
            "--unshare-user",
            "--unshare-pid",
            "--new-session",
        ]

        # Map to userns-root when the policy needs effective capabilities inside
        # the namespace (the netns sole-egress chain). MUST precede --cap-add:
        # bwrap's default --unshare-user keeps the caller's uid (not userns-root)
        # so CAP_NET_ADMIN would be silently dropped without this mapping.
        if policy.uid_root:
            argv += ["--uid", "0", "--gid", "0"]

        # Read-only host root baseline (must precede --proc/--dev).
        argv += ["--ro-bind", "/", "/"]

        # Fresh writable proc + dev on top of the read-only root.
        argv += [
            "--proc",
            "/proc",
            "--dev",
            "/dev",
        ]

        # Private writable scratch. Bound BEFORE the writable holes so a writable
        # root that lives UNDER /tmp (e.g. a tmp-dir workspace) layers on top of
        # the tmpfs rather than being masked by it (bwrap applies mounts in order;
        # --tmpfs /tmp after a --bind /tmp/work would hide /tmp/work).
        argv += ["--tmpfs", "/tmp"]

        # Writable holes (deduped, only existing dirs — bwrap errors on a
        # missing bind source).
        seen: set[str] = set()
        for root in policy.writable_roots:
            real = os.path.realpath(root)
            if real in seen or not os.path.exists(real):
                continue
            seen.add(real)
            argv += ["--bind", real, real]

        # Extra writable binds (e.g. an orchestrator scratch dir) — same
        # existence/dedup rules as the writable roots.
        for extra in policy.extra_writable:
            real = os.path.realpath(extra)
            if real in seen or not os.path.exists(real):
                continue
            seen.add(real)
            argv += ["--bind", real, real]

        # Re-pin sensitive paths read-only (anti-escape). Applied AFTER the
        # writable binds so the last matching bind for an overlapping path wins
        # (bwrap applies binds in order).
        for path in policy.readonly_overrides:
            real = os.path.realpath(path)
            if not os.path.exists(real):
                continue
            argv += ["--ro-bind", real, real]

        if policy.unshare_net:
            argv += ["--unshare-net"]

        # Grant CAP_NET_ADMIN so the userns-root inner process can configure its
        # own network namespace (bring lo up, install nft rules). Only meaningful
        # alongside --unshare-net; used by the proxy-whitelist netns chain.
        if policy.cap_net_admin:
            argv += ["--cap-add", "CAP_NET_ADMIN"]

        # Device passthrough (e.g. /dev/net/tun for slirp4netns userspace egress).
        for dev in policy.dev_binds:
            if os.path.exists(dev):
                argv += ["--dev-bind", dev, dev]

        # seccomp BPF program (dangerous-syscall hardening and/or inet block),
        # read from a file descriptor the spawn site redirects from the compiled
        # BPF file. A single-digit fd keeps the redirect dash-compatible.
        if policy.seccomp_fd is not None:
            argv += ["--seccomp", str(policy.seccomp_fd)]

        # Report runtime info (incl. child-pid) on this fd so the netns
        # orchestrator can attach slirp4netns to the inner command's namespace.
        if policy.info_fd is not None:
            argv += ["--info-fd", str(policy.info_fd)]

        # Run inside the workspace when we have one.
        if policy.cwd and os.path.isdir(policy.cwd):
            argv += ["--chdir", policy.cwd]

        argv += ["--"]
        argv += list(inner_argv)
        return argv


__all__ = ["BwrapBackend"]
