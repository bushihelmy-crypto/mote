"""Netns orchestration — the stateful half of the P2 sole-egress chain.

:mod:`.enforce` provides the pure builders (toolchain probe, slirp argv, nft
ruleset, inner prelude). This module wires them into a runnable chain.

The problem: our spawn seam is dead simple — ``wrap_command`` returns a shell
string for ``create_subprocess_shell`` and ``wrap_exec`` returns an argv for
``create_subprocess_exec``. But the netns chain needs *coordination*: spawn
bwrap, read the child-pid it reports on ``--info-fd``, spawn ``slirp4netns``
attached to that pid, wait for the tap to come up, then let the payload run.

The solution: emit a **launcher** invocation instead of bwrap directly. The
runtime returns ``python3 -m mote.sandbox.network.orchestrator <b64-config>``
(as a string for Bash, or an argv for the PTY/exec seam). That launcher process
— this module's ``main`` — does the coordination, inheriting the caller's
stdio (so a PTY flows straight through to the inner command) while using a
*separate* pipe fd for ``--info-fd`` (verified orthogonal to the PTY).

Config is passed base64-encoded on argv to dodge all shell-quoting issues; it
carries the full bwrap argv (built by the backend, single source of truth), the
fd numbers to wire, the proxy port, and the optional seccomp BPF path.

Layering: imports only stdlib + sibling ``enforce`` (which imports only stdlib).
The runtime façade builds the config; this module is also runnable as ``__main__``
inside the spawned launcher.
"""
from __future__ import annotations

import base64
import json
import os
import select
import shlex
import subprocess
import sys
import time
from typing import Optional

from mote.sandbox.network.enforce import build_slirp_argv

# Placeholder fd the backend bakes into the bwrap argv as ``--info-fd N``. The
# launcher patches in the *real* inherited pipe fd at runtime (see
# ``_replace_flag_value``), so this is only a syntactic placeholder — 3 is the
# first fd after std{in,out,err}.
INFO_FD = 3

# Module path used to invoke the launcher (``python -m <this>``).
_LAUNCHER_MODULE = "mote.sandbox.network.orchestrator"


def build_inner_argv(prelude: str, payload_argv: list[str]) -> list[str]:
    """Wrap *payload_argv* so the netns prelude runs first, then ``exec``s it.

    ``sh -c '<prelude; exec "$@">' sbx <payload...>`` — the prelude brings the
    namespace up + installs the nft lock, then ``exec "$@"`` replaces the shell
    with the real payload (so PTY/signal routing reaches it with no lingering
    wrapper). ``sbx`` is ``$0``; the payload becomes ``$@``.
    """
    return ["/bin/sh", "-c", prelude, "sbx", *payload_argv]


def encode_config(
    *,
    bwrap_argv: list[str],
    proxy_port: int,
    seccomp_path: Optional[str] = None,
    seccomp_fd: Optional[int] = None,
) -> str:
    """Serialise the launcher config to a base64 token (argv-safe)."""
    payload = {
        "bwrap_argv": bwrap_argv,
        "proxy_port": proxy_port,
        "seccomp_path": seccomp_path,
        "seccomp_fd": seccomp_fd,
    }
    raw = json.dumps(payload).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def launcher_argv(config_token: str) -> list[str]:
    """The argv that runs the launcher with *config_token* (for ``wrap_exec``)."""
    return [sys.executable, "-m", _LAUNCHER_MODULE, config_token]


def launcher_command(config_token: str) -> str:
    """The shell string that runs the launcher (for ``wrap_command``)."""
    return " ".join(shlex.quote(a) for a in launcher_argv(config_token))


# --- launcher (runs as __main__ in the spawned process) --------------------


def _read_child_pid(info_r: int, *, timeout: float = 5.0) -> Optional[int]:
    """Read the bwrap ``child-pid`` from the info pipe read-end (best-effort)."""
    buf = b""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r, _, _ = select.select([info_r], [], [], 0.2)
        if info_r not in r:
            continue
        try:
            chunk = os.read(info_r, 4096)
        except OSError:
            return None
        if not chunk:
            break
        buf += chunk
        try:
            return int(json.loads(buf.decode("utf-8")).get("child-pid"))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return None


def _replace_flag_value(argv: list[str], flag: str, value: str) -> list[str]:
    """Return *argv* with the token following *flag* replaced by *value*.

    The backend bakes placeholder fd numbers (``--info-fd 3`` / ``--seccomp 9``)
    into the argv, but the launcher only learns the *real* inherited fd numbers
    at runtime. Rather than force fds onto fixed numbers (a ``dup2`` dance that
    proved unreliable — bwrap saw a closed fd 3), we keep the pipe/file at its
    natural number, mark it inheritable, and patch the flag value here. No-op
    when the flag is absent.
    """
    out = list(argv)
    try:
        i = out.index(flag)
    except ValueError:
        return out
    if i + 1 < len(out):
        out[i + 1] = value
    return out


def _run_launcher(config_token: str) -> int:
    """Coordinate bwrap + slirp4netns, inheriting our stdio. Returns rc.

    Steps:
      1. open the info pipe; mark its write-end inheritable and patch the real
         fd number into the baked ``--info-fd`` flag,
      2. (optional) open the seccomp BPF read-only, mark it inheritable, patch
         the real fd into ``--seccomp``,
      3. spawn bwrap inheriting our std{in,out,err} (PTY flows through) + those
         fds (``pass_fds``),
      4. read the child-pid off the info pipe,
      5. spawn ``slirp4netns`` attached to that pid; wait its ready byte,
      6. wait for bwrap to exit; tear slirp down.
    """
    cfg = json.loads(base64.b64decode(config_token).decode("utf-8"))
    bwrap_argv: list[str] = cfg["bwrap_argv"]
    seccomp_path: Optional[str] = cfg.get("seccomp_path")
    seccomp_fd: Optional[int] = cfg.get("seccomp_fd")

    info_r, info_w = os.pipe()
    ready_r, ready_w = os.pipe()
    os.set_inheritable(info_w, True)
    os.set_inheritable(ready_w, True)

    # Patch the real info-fd number into the argv (the backend baked a
    # placeholder). bwrap writes its child-pid JSON to this inherited fd.
    bwrap_argv = _replace_flag_value(bwrap_argv, "--info-fd", str(info_w))

    pass_fds = [info_w]
    seccomp_src = None
    if seccomp_path and seccomp_fd is not None:
        try:
            seccomp_src = os.open(seccomp_path, os.O_RDONLY)
            os.set_inheritable(seccomp_src, True)
            bwrap_argv = _replace_flag_value(bwrap_argv, "--seccomp", str(seccomp_src))
            pass_fds.append(seccomp_src)
        except OSError:
            seccomp_src = None

    proc = subprocess.Popen(
        bwrap_argv,
        stdin=0,
        stdout=1,
        stderr=2,
        pass_fds=tuple(pass_fds),
        close_fds=True,
    )
    os.close(info_w)
    if seccomp_src is not None:
        os.close(seccomp_src)

    child_pid = _read_child_pid(info_r)
    os.close(info_r)

    slirp = None
    if child_pid is not None:
        try:
            slirp = subprocess.Popen(
                build_slirp_argv(child_pid, ready_fd=ready_w),
                pass_fds=(ready_w,),
                close_fds=True,
            )
            os.close(ready_w)
            # Block until slirp signals the tap is configured (one byte), so the
            # inner prelude's route-wait doesn't spin needlessly. Best-effort.
            try:
                os.read(ready_r, 16)
            except OSError:
                pass
        except OSError:
            # slirp failed to spawn — the inner prelude's route-wait will time
            # out and the nft lock won't see a gateway, but the payload still
            # runs (degraded to env-proxy only, never a hard break).
            try:
                os.close(ready_w)
            except OSError:
                pass
    else:
        try:
            os.close(ready_w)
        except OSError:
            pass
    try:
        os.close(ready_r)
    except OSError:
        pass

    try:
        rc = proc.wait()
    finally:
        if slirp is not None:
            slirp.terminate()
            try:
                slirp.wait(timeout=3)
            except subprocess.TimeoutExpired:
                slirp.kill()
    return rc


def main(argv: Optional[list[str]] = None) -> int:
    """Entry point: ``python -m ...orchestrator <config_token>``."""
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print("orchestrator: missing config token", file=sys.stderr)
        return 2
    return _run_launcher(args[0])


__all__ = [
    "INFO_FD",
    "build_inner_argv",
    "encode_config",
    "launcher_argv",
    "launcher_command",
    "main",
]


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    raise SystemExit(main())
