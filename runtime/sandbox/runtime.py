"""SandboxRuntime — the façade the executor injects and calls.

Owns the lifecycle of the OS-level sandbox for one Role session:

  * :meth:`start` — probe the backend; start the egress proxy (when networking
    is enforced). Idempotent.
  * :meth:`wrap_command` — the main seam for ``Bash``: take a shell command
    string + cwd + env, return a ``(wrapped_command, env)`` pair where the
    command is bwrap-wrapped around a hardening prelude and the env carries the
    proxy variables.
  * :meth:`wrap_exec` — the seam for the PTY terminal: take an argv list
    (``[shell, *args]``) and return a bwrap-wrapped ``(argv, env)``.
  * :meth:`parse_violations` — lift a command's stderr into structured
    :class:`SandboxViolation`\\s.
  * :meth:`shutdown` — stop the proxy. Idempotent.

Degradation: when the requested backend is unavailable,
``fail_if_unavailable`` decides between a hard ``RuntimeError`` and a silent
passthrough (the command runs unsandboxed, with a warning logged).

Policy source: the runtime does NOT compute writable roots itself. A
``policy_provider`` callable (wired by the adapter) returns a fresh
:class:`~mote.runtime.sandbox.backend.SandboxPolicy` per call, derived from the live
``SandboxGuard`` + ``SandboxRuntimeConfig`` — so a session-granted writable root
takes effect on the next command without rebuilding the runtime.
"""
from __future__ import annotations

import os
import shlex
from typing import TYPE_CHECKING, Callable, Optional

from mote.runtime.logging import logger
from mote.runtime.sandbox.backend import NullBackend, SandboxBackend, SandboxPolicy
from mote.runtime.sandbox.bwrap import BwrapBackend
from mote.runtime.sandbox.detect import detect_backend
from mote.runtime.sandbox.hardening import harden_env, hardening_prelude
from mote.runtime.sandbox.network.enforce import (
    TUN_DEVICE,
    build_inner_prelude,
    enforcement_available,
    proxy_url_in_netns,
)
from mote.runtime.sandbox.network.netns import block_all_network_env, inject_proxy_env
from mote.runtime.sandbox.network.orchestrator import build_inner_argv, encode_config, launcher_argv, launcher_command
from mote.runtime.sandbox.network.policy import NetworkPolicy
from mote.runtime.sandbox.network.proxy import EgressProxy
from mote.runtime.sandbox.network.tls import MitmCa
from mote.runtime.sandbox.resources import (
    ResourceLimits,
    cgroup_limits_available,
    cpu_controller_delegated,
    rlimit_prelude,
    systemd_run_prefix,
)
from mote.runtime.sandbox.seccomp import build_hardening_filter, seccomp_available
from mote.runtime.sandbox.violations import SandboxViolation, parse_violations

if TYPE_CHECKING:
    from mote.runtime.sandbox.network.credentials import CredentialBroker

# The shell used to run the hardening prelude + inner command inside the sandbox.
_INNER_SHELL = "/bin/sh"

# Single-digit fd the compiled seccomp BPF is redirected onto before bwrap reads
# it. MUST be single-digit: dash (the usual /bin/sh) rejects multi-digit fds in
# redirections ("Bad fd number"), and wrap_command emits a `sh -c` string.
_SECCOMP_FD = 9

# Fd bwrap reports its child-pid on (``--info-fd``) under the netns chain. The
# orchestrator launcher dups its info-pipe write-end onto this number; mirrors
# ``orchestrator.INFO_FD`` (kept here to avoid importing it on the hot path).
_INFO_FD = 3


class SandboxRuntime:
    """Per-session façade orchestrating bwrap + hardening + network proxy."""

    def __init__(
        self,
        *,
        backend: str = "auto",
        fail_if_unavailable: bool = False,
        harden_process: bool = True,
        seccomp: bool = True,
        network: str = "proxy",
        network_enforcement: bool = True,
        allowed_domains: Optional[list[str]] = None,
        memory_max: Optional[str] = None,
        pids_max: Optional[int] = None,
        cpu_quota: Optional[str] = None,
        policy_provider: Optional[Callable[[], SandboxPolicy]] = None,
        limits_provider: Optional[Callable[[], ResourceLimits]] = None,
        credential_broker: Optional["CredentialBroker"] = None,
    ) -> None:
        self._requested_backend = backend
        self._fail_if_unavailable = fail_if_unavailable
        self._harden = harden_process
        self._seccomp = seccomp
        self._network = network
        self._network_enforcement = network_enforcement
        self._allowed_domains = list(allowed_domains or [])
        self._policy_provider = policy_provider
        # Optional per-domain credential broker (injects an auth header at the
        # proxy for configured hosts). None => the proxy runs unchanged. When it
        # has intercept hosts, HTTPS MITM + trust-anchor env engage.
        self._credential_broker = credential_broker
        # Group-level (process-tree) resource caps applied via the outermost
        # ``systemd-run --user --scope`` wrapper. Orthogonal to the isolation
        # backend: even a NullBackend command gets the cgroup limits.
        #
        # Like ``policy_provider``, ``limits_provider`` (when wired by the
        # adapter) returns a FRESH :class:`ResourceLimits` per call, so a
        # session-adjusted cap (e.g. an interactive "raise memory to 8G") takes
        # effect on the very next command without rebuilding the runtime. The
        # static ``memory_max``/``pids_max``/``cpu_quota`` kwargs are the
        # fallback baseline used when no provider is wired.
        self._limits_provider = limits_provider
        self._static_limits = ResourceLimits(
            memory_max=memory_max,
            cpu_quota=cpu_quota,
            pids_max=pids_max,
        )

        self._backend: SandboxBackend = NullBackend()
        self._proxy: Optional[EgressProxy] = None
        # Local MITM CA, built lazily at start() only when the broker has
        # intercept hosts (Phase 2 HTTPS interception + trust-anchor bundle).
        self._mitm_ca: Optional["MitmCa"] = None
        self._started = False
        # Path to the compiled seccomp BPF (hardening filter), built once at
        # start() when seccomp is enabled and available. None => no filter.
        self._seccomp_bpf_path: Optional[str] = None
        # Whether the netns sole-egress chain is active for this session: set at
        # start() when network=="proxy", enforcement is requested, the bwrap
        # backend is live, and the slirp/nft toolchain is present. When True,
        # wrap_command/wrap_exec emit the orchestrator launcher (a real netns
        # whose only egress is the proxy) instead of mere env-var injection.
        self._netns_egress = False
        # Host capability for cgroup limits, probed once at start() (the systemd
        # user manager + cgroup2 + cpu-delegation are host facts that don't
        # change per command). The limit VALUES are recomputed per wrap from the
        # provider, so only the host capability is cached here.
        self._cgroup_available = False
        self._cpu_delegated = False

    # --- lifecycle ---------------------------------------------------------

    @property
    def backend_name(self) -> str:
        return self._backend.name

    async def start(self) -> None:
        """Probe the backend and start the egress proxy. Idempotent."""
        if self._started:
            return
        self._started = True

        resolved = detect_backend(self._requested_backend)
        if resolved == "bwrap":
            backend = BwrapBackend()
            if backend.available:
                self._backend = backend
            else:
                self._handle_unavailable("bwrap")
        else:
            # Either explicitly "none" or auto-probe found nothing usable.
            if self._requested_backend not in ("auto", "none"):
                self._handle_unavailable(self._requested_backend)
            self._backend = NullBackend()

        # Build the seccomp hardening filter once (static, command-independent)
        # when enabled, available, and we actually have a bwrap backend to carry
        # it. NullBackend has no --seccomp, so there is nothing to attach.
        if self._seccomp and not isinstance(self._backend, NullBackend) and seccomp_available():
            self._seccomp_bpf_path = build_hardening_filter()

        # Probe the host's cgroup-limit capability ONCE (the systemd user
        # manager, cgroup2 mount and cpu delegation are host facts, not
        # per-command). The limit VALUES are read fresh per wrap from the
        # provider, so only the capability is cached. Orthogonal to the backend:
        # a NullBackend command still gets resource caps (DoS protection does
        # not depend on bwrap). Warn once based on the baseline limits.
        baseline = self._current_limits()
        if not baseline.is_empty:
            self._cgroup_available = cgroup_limits_available()
            if self._cgroup_available:
                self._cpu_delegated = cpu_controller_delegated()
                if baseline.cpu_quota is not None and not self._cpu_delegated:
                    logger.warning(
                        "SandboxRuntime: CPUQuota requested but the 'cpu' "
                        "controller is not delegated to the user scope; the "
                        "quota would be ignored, so it is dropped. To enable it, "
                        "root must add 'Delegate=cpu …' under "
                        "/etc/systemd/system/user@.service.d/ and run "
                        "'systemctl daemon-reexec'."
                    )
            else:
                logger.warning(
                    "SandboxRuntime: cgroup resource limits unavailable "
                    "(no systemd user manager / cgroup v2); running without "
                    "resource caps (degraded)."
                )

        if self._network == "proxy":
            # Build the MITM CA up front when the broker will intercept HTTPS,
            # so the proxy can mint per-host leaf certs and the trust-anchor env
            # can point tools at the combined bundle.
            if self._credential_broker is not None and self._credential_broker.intercept_hosts:
                try:
                    self._mitm_ca = MitmCa()
                except Exception as exc:  # noqa: BLE001 — degrade to HTTP-only brokering
                    logger.warning(f"SandboxRuntime: MITM CA init failed ({exc}); HTTPS brokering disabled")
                    self._mitm_ca = None
            self._proxy = EgressProxy(
                NetworkPolicy(self._allowed_domains),
                broker=self._credential_broker,
                mitm_ca=self._mitm_ca,
            )
            await self._proxy.start()
            # Upgrade to a real netns sole-egress chain when requested, we have
            # a bwrap backend to host it, and the slirp/nft toolchain exists.
            # Otherwise stay on env-var proxy injection (P1 behaviour).
            if self._network_enforcement and not isinstance(self._backend, NullBackend):
                self._netns_egress = enforcement_available()

    def _handle_unavailable(self, name: str) -> None:
        """React to a requested-but-unavailable backend per the fail policy."""
        msg = f"sandbox backend '{name}' is not available on this host"
        if self._fail_if_unavailable:
            raise RuntimeError(msg)
        logger.warning(f"SandboxRuntime: {msg}; running unsandboxed (degraded)")
        self._backend = NullBackend()

    async def shutdown(self) -> None:
        """Stop the egress proxy + remove the seccomp BPF file (idempotent)."""
        if self._proxy is not None:
            await self._proxy.shutdown()
            self._proxy = None
        if self._seccomp_bpf_path is not None:
            try:
                os.unlink(self._seccomp_bpf_path)
            except OSError:
                pass
            self._seccomp_bpf_path = None
        self._netns_egress = False
        self._cgroup_available = False
        self._cpu_delegated = False
        self._started = False

    # --- command wrapping --------------------------------------------------

    def _policy_for(self, cwd: Optional[str], *, extra_writable: Optional[list[str]] = None) -> SandboxPolicy:
        """Build the policy for this call (from the provider, or a cwd-only default)."""
        if self._policy_provider is not None:
            policy = self._policy_provider()
        else:
            policy = SandboxPolicy()
        if cwd:
            policy.cwd = cwd
            if cwd not in policy.writable_roots:
                policy.writable_roots = [*policy.writable_roots, cwd]
        # Extra writable bind mounts (e.g. the Jupyter kernel's ipc:// socket
        # directory, which the host client + sandboxed kernel must share). The
        # backend emits a ``--bind`` per path; NullBackend ignores the policy, so
        # this is a natural no-op when sandboxing is degraded.
        if extra_writable:
            policy.extra_writable = [*policy.extra_writable, *extra_writable]
        # Hard network-off: unshare the net namespace (external egress dies with
        # ENETUNREACH). A sandboxed kernel is unaffected — its ipc:// unix-socket
        # channels are filesystem-bound, not loopback TCP.
        if self._network == "off":
            policy.unshare_net = True
        # Proxy sole-egress: a fresh netns whose only route out is the proxy. The
        # inner process must be userns-root with CAP_NET_ADMIN to bring up lo +
        # install the nft lock, and needs /dev/net/tun for slirp4netns.
        if self._netns_egress:
            policy.unshare_net = True
            policy.uid_root = True
            policy.cap_net_admin = True
            if TUN_DEVICE not in policy.dev_binds:
                policy.dev_binds = [*policy.dev_binds, TUN_DEVICE]
            policy.info_fd = _INFO_FD
        # Attach the seccomp hardening filter when one was built. The spawn-side
        # wrappers (wrap_command / wrap_exec) redirect fd _SECCOMP_FD from the
        # BPF file so bwrap can read it.
        if self._seccomp_bpf_path is not None:
            policy.seccomp_fd = _SECCOMP_FD
        return policy

    def _apply_network_env(self, env: dict[str, str]) -> dict[str, str]:
        """Layer the network env policy onto *env*."""
        if self._network == "proxy" and self._proxy is not None:
            if self._netns_egress:
                # Inside the netns the host's 127.0.0.1 is unreachable; the proxy
                # is only reachable via the slirp gateway. Point proxy-aware
                # tools at the gateway URL (the nft lock backstops everything
                # else regardless of whether the tool honours the proxy).

                out = inject_proxy_env(env, proxy_url_in_netns(self._proxy.port))
            else:
                out = inject_proxy_env(env, self._proxy.url)
            return self._apply_trust_anchor_env(out)
        if self._network == "off":
            return block_all_network_env(env)
        return env  # "open" — no network env changes

    def _apply_trust_anchor_env(self, env: dict[str, str]) -> dict[str, str]:
        """Point TLS-using tools at the combined CA bundle (MITM interception).

        A no-op unless a MITM CA is active (the broker has intercept hosts). When
        it is, sandboxed tools must trust the per-host leaf we mint for
        credentialed domains, so every common trust-anchor var is set to the
        combined bundle (real public roots **plus** our CA) and ``SSL_CERT_DIR``
        is cleared so a stale hashed-dir store can't shadow the bundle. The bundle
        lives under ``~/.mote/`` — already visible read-only inside the sandbox
        via the ``--ro-bind / /`` baseline (the CA *private* key is masked). Every
        non-MITM'd origin still validates against the real roots in the bundle, so
        interception stays scoped to exactly the configured domains.
        """
        if self._mitm_ca is None:
            return env
        try:
            bundle = self._mitm_ca.combined_bundle_path()
        except Exception as exc:  # noqa: BLE001 — never break the command path
            logger.warning(f"SandboxRuntime: trust-anchor bundle unavailable ({exc}); skipping")
            return env
        out = dict(env)
        for var in ("SSL_CERT_FILE", "GIT_SSL_CAINFO", "CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS"):
            out[var] = bundle
        out.pop("SSL_CERT_DIR", None)
        return out

    def _rlimit_prelude_now(self) -> str:
        """Per-process ``ulimit`` fallback snippet for the live limits, or "".

        The weaker per-process backstop for hosts where the cgroup scope
        degrades to a no-op. Engaged ONLY when ``cgroup_available`` is False —
        when the systemd-run scope is active it already caps the whole tree, and
        layering a redundant ulimit on top could clash. Reads the live limits so
        a session adjustment is honoured on the next command (mirrors
        ``_cgroup_prefix_now``).
        """
        if self._cgroup_available:
            return ""
        return rlimit_prelude(self._current_limits())

    def _shell_prelude(self) -> str:
        """Combine the hardening + rlimit-fallback preludes (``;``-joined, or "").

        Two independent gates: the hardening prelude (core dumps off, ``LD_*``
        stripped) when ``harden_process``; the per-process resource fallback when
        the cgroup scope is unavailable. Resource caps are orthogonal to
        hardening — the rlimit fallback applies even with ``harden_process=False``
        (just as the cgroup scope does).
        """
        parts: list[str] = []
        if self._harden:
            parts.append(hardening_prelude())
        rl = self._rlimit_prelude_now()
        if rl:
            parts.append(rl)
        return "; ".join(parts)

    def _inner_shell_argv(self, command: str) -> list[str]:
        """Wrap *command* in ``/bin/sh -c`` with the shell prelude prepended."""
        prelude = self._shell_prelude()
        if prelude:
            command = f"{prelude}; {command}"
        return [_INNER_SHELL, "-c", command]

    def _build_netns_bwrap_argv(
        self,
        payload_argv: list[str],
        cwd: Optional[str],
        extra_writable: Optional[list[str]] = None,
    ) -> list[str]:
        """Build the bwrap argv for the netns chain wrapping *payload_argv*.

        The inner command is the netns prelude (lo up → wait route → nft lock →
        ``exec "$@"``) with *payload_argv* as ``$@``. The policy (built by
        ``_policy_for`` in egress mode) carries uid_root/cap_net_admin/dev_binds/
        info_fd, so the backend emits ``--uid 0 --gid 0 --cap-add ... --dev-bind
        /dev/net/tun --info-fd 3``. ``extra_writable`` (e.g. the kernel ipc://
        socket dir) is bound read-write inside the netns sandbox too.
        """

        assert self._proxy is not None  # guarded by callers
        prelude = build_inner_prelude(self._proxy.port)
        inner = build_inner_argv(prelude, payload_argv)
        policy = self._policy_for(cwd, extra_writable=extra_writable)
        return self._backend.build_argv(policy, inner)

    def _netns_config_token(self, bwrap_argv: list[str]) -> str:
        """Encode the launcher config (bwrap argv + proxy port + seccomp path)."""

        assert self._proxy is not None
        return encode_config(
            bwrap_argv=bwrap_argv,
            proxy_port=self._proxy.port,
            seccomp_path=self._seccomp_bpf_path,
            seccomp_fd=_SECCOMP_FD if self._seccomp_bpf_path is not None else None,
        )

    def _build_netns_launcher_command(self, payload_argv: list[str], cwd: Optional[str]) -> Optional[str]:
        """Return the launcher *shell string* for ``wrap_command`` (or None)."""
        try:
            bwrap_argv = self._build_netns_bwrap_argv(payload_argv, cwd)
            return launcher_command(self._netns_config_token(bwrap_argv))
        except Exception as exc:  # noqa: BLE001 — never break the command path
            logger.warning(f"SandboxRuntime: netns launcher build failed ({exc}); " "falling back to direct bwrap")
            return None

    def _build_netns_launcher_argv(
        self,
        payload_argv: list[str],
        cwd: Optional[str],
        extra_writable: Optional[list[str]] = None,
    ) -> Optional[list[str]]:
        """Return the launcher *argv* for ``wrap_exec`` (or None)."""
        try:
            bwrap_argv = self._build_netns_bwrap_argv(payload_argv, cwd, extra_writable)
            return launcher_argv(self._netns_config_token(bwrap_argv))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"SandboxRuntime: netns launcher build failed ({exc}); " "falling back to direct bwrap")
            return None

    # --- cgroup resource limits --------------------------------------------

    def _current_limits(self) -> ResourceLimits:
        """The live :class:`ResourceLimits` for this call.

        Reads from the ``limits_provider`` when wired (so a session-adjusted cap
        is honoured on the next command), else the static baseline. Mirrors
        ``_policy_for``'s provider-or-default pattern.
        """
        if self._limits_provider is not None:
            return self._limits_provider()
        return self._static_limits

    def _cgroup_prefix_now(self) -> list[str]:
        """Compute the ``systemd-run`` scope prefix for the live limits.

        Returns ``[]`` when the host can't apply limits (probed at start()) or
        the live limits are empty. The cpu controller's delegation was probed
        once at start(); the limit values are read fresh here so a runtime
        adjustment takes effect on the next command.
        """
        if not self._cgroup_available:
            return []
        limits = self._current_limits()
        if limits.is_empty:
            return []
        return systemd_run_prefix(limits, with_cpu=self._cpu_delegated)

    def _apply_cgroup_command(self, wrapped: str) -> str:
        """Prepend the ``systemd-run`` scope prefix to a shell command *string*.

        No-op when the live prefix is empty (empty limits or unavailable host).
        Otherwise the prefix argv is shell-quoted and prepended, so the whole
        command (launcher / bwrap / payload tree) runs inside the transient
        scope's cgroup.
        """
        prefix_argv = self._cgroup_prefix_now()
        if not prefix_argv:
            return wrapped
        prefix = " ".join(shlex.quote(a) for a in prefix_argv)
        return f"{prefix} {wrapped}"

    def _apply_cgroup_argv(self, argv: list[str]) -> list[str]:
        """Prepend the ``systemd-run`` scope prefix to an *argv* list.

        No-op when the live prefix is empty. ``systemd-run --scope`` forwards
        inherited fds (seccomp BPF / netns ``pass_fds``), stdin/stdout/stderr
        and the PTY to the scoped command, so this composes with every seam.
        """
        prefix_argv = self._cgroup_prefix_now()
        if not prefix_argv:
            return argv
        return [*prefix_argv, *argv]

    async def wrap_command(
        self,
        command: str,
        *,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
    ) -> tuple[str, dict[str, str]]:
        """Wrap a shell *command* string for sandboxed execution.

        Returns ``(wrapped_command, env)``:
          * ``wrapped_command`` — a single shell-quoted string ready for
            ``create_subprocess_shell``. With bwrap active it is
            ``bwrap <flags> -- /bin/sh -c '<prelude>; <command>'``; with the
            null backend it is just the hardened ``/bin/sh -c`` form (or the
            original command when hardening is off too).
          * ``env`` — the original env (or the process env) with hardening +
            network policy applied.
        """
        await self.start()
        base_env = harden_env(env) if (self._harden and env is not None) else (env or {})
        # When no env was supplied, fall back to a copy of the current process
        # env so proxy/hardening edits have something to layer onto.
        if env is None:
            base_env = harden_env(dict(os.environ)) if self._harden else dict(os.environ)
        out_env = self._apply_network_env(base_env)

        # Netns sole-egress: emit the orchestrator launcher instead of running
        # bwrap directly. The launcher spawns bwrap (with --info-fd baked in),
        # reads the child-pid, attaches slirp4netns, and lets the inner prelude
        # install the nft lock before exec'ing the payload. The seccomp BPF is
        # passed by path (the launcher dup2's it onto the fd; no shell redirect).
        if self._netns_egress and self._proxy is not None:
            payload = self._inner_shell_argv(command)
            launcher = self._build_netns_launcher_command(payload, cwd)
            if launcher is not None:
                return self._apply_cgroup_command(launcher), out_env

        inner = self._inner_shell_argv(command)
        policy = self._policy_for(cwd)
        argv = self._backend.build_argv(policy, inner)

        if isinstance(self._backend, NullBackend) and not self._shell_prelude():
            # Nothing to inject — no isolation backend, no hardening, no rlimit
            # fallback. True passthrough (only the env policy applies).
            prefix_argv = self._cgroup_prefix_now()
            if not prefix_argv:
                return command, out_env
            # With a cgroup scope active we must still confine the whole command
            # (incl. shell operators like ``&&``/``;``) to the scope. Prepending
            # the prefix to a raw string would let only the first segment run in
            # the scope, so route the command through ``sh -c`` as the scoped
            # command: ``systemd-run … /bin/sh -c '<command>'``.
            scoped = [*prefix_argv, _INNER_SHELL, "-c", command]
            return " ".join(shlex.quote(a) for a in scoped), out_env

        wrapped = " ".join(shlex.quote(a) for a in argv)
        # Prepend the cgroup scope (outermost) BEFORE appending the seccomp
        # redirect, so the BPF ``9<path`` redirect stays at the very end of the
        # string (verified: fd9 passes through systemd-run into the scope into
        # bwrap). Order: ``systemd-run … bwrap … 9<path``.
        wrapped = self._apply_cgroup_command(wrapped)
        # When the policy carries a seccomp fd, redirect that single-digit fd
        # from the compiled BPF file so bwrap can read it. Appending the redirect
        # to the whole command string applies it to the bwrap process. Uses a
        # single-digit fd (dash rejects multi-digit fds in redirections).
        if policy.seccomp_fd is not None and self._seccomp_bpf_path is not None:
            wrapped = f"{wrapped} {policy.seccomp_fd}<{shlex.quote(self._seccomp_bpf_path)}"
        return wrapped, out_env

    async def wrap_exec(
        self,
        argv: list[str],
        *,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        extra_writable: Optional[list[str]] = None,
    ) -> tuple[list[str], dict[str, str]]:
        """Wrap an *argv* list for sandboxed execution (PTY terminal seam).

        Unlike :meth:`wrap_command`, the inner command is an argv (e.g.
        ``["/bin/bash", "--norc", ...]``), so we do NOT route it through
        ``/bin/sh -c`` — bwrap exec's it directly. Process hardening for this
        path relies on the loader-var stripping in the env (the full prelude
        needs a shell ``-c``, which we don't impose on an interactive shell's
        argv). The per-process ``ulimit`` resource fallback DOES apply though:
        when the cgroup scope is unavailable it is set in a thin ``sh -c`` shim
        that then ``exec``'s the argv, so the shell + descendants inherit it.

        ``extra_writable`` lists absolute paths to bind read-write inside the
        sandbox beyond the policy's writable roots. The Jupyter kernel uses it to
        expose its ipc:// unix-socket directory so the host client and the
        sandboxed kernel share the same socket inodes across the netns boundary.

        Returns ``(argv, env)``. With the null backend the argv is returned
        unchanged (only env policy applies).
        """
        await self.start()
        if env is None:
            base = dict(os.environ)
        else:
            base = dict(env)
        if self._harden:
            base = harden_env(base)
        out_env = self._apply_network_env(base)

        # Netns sole-egress: return the launcher argv (PTY flows through it to
        # the inner command, --info-fd uses a separate pipe — verified
        # orthogonal to the PTY).
        if self._netns_egress and self._proxy is not None:
            launcher = self._build_netns_launcher_argv(argv, cwd, extra_writable)
            if launcher is not None:
                return self._apply_cgroup_argv(launcher), out_env

        policy = self._policy_for(cwd, extra_writable=extra_writable)
        wrapped = self._backend.build_argv(policy, argv)
        # Two reasons to wrap the argv in a tiny ``sh -c '… exec "$@" …'`` shim
        # (``exec`` leaves no lingering shell, so PTY/signal routing survives):
        #   * seccomp: bwrap must read the BPF from a fd, so redirect it
        #     (``N<path``) — non-null backend only (nothing to seccomp on null).
        #   * rlimit fallback: when the cgroup scope is unavailable, set the
        #     per-process ``ulimit`` caps in the shell so the exec'd shell + all
        #     its descendants inherit them (the argv path has no other prelude
        #     seam — unlike ``wrap_command``'s ``sh -c`` body). Applies for any
        #     backend (resource caps are orthogonal to isolation).
        rlimit = self._rlimit_prelude_now()
        seccomp_redirect = (
            policy.seccomp_fd is not None
            and self._seccomp_bpf_path is not None
            and not isinstance(self._backend, NullBackend)
        )
        if rlimit or seccomp_redirect:
            redirect = f" {policy.seccomp_fd}<{shlex.quote(self._seccomp_bpf_path or '')}" if seccomp_redirect else ""
            prefix = f"{rlimit}; " if rlimit else ""
            shim = f'{prefix}exec "$@"{redirect}'
            wrapped = [_INNER_SHELL, "-c", shim, "sbx", *wrapped]
        # Prepend the cgroup scope as the outermost wrapper (after the shim is
        # assembled). ``pass_fds`` + PTY + the fd9 redirect all pass through
        # ``systemd-run --scope`` into the scoped command (verified). Mutually
        # exclusive with the rlimit fallback: when the scope is active the
        # cgroup caps the tree, so ``_rlimit_prelude_now`` returns "".
        return self._apply_cgroup_argv(wrapped), out_env

    # --- diagnostics -------------------------------------------------------

    def parse_violations(self, stderr: str) -> list[SandboxViolation]:
        """Lift sandbox-related errors out of a command's *stderr*."""
        return parse_violations(stderr)


__all__ = ["SandboxRuntime"]
