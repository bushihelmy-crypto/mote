"""Shell-command parsing for the permission layer.

A small, dependency-free toolkit shared by the classifier (``classifier.py``),
the rule matcher (prefix rules), and the permission engine (per-segment
evaluation). It does three things, all best-effort:

  * **split** a command line into independent segments on the shell operators
    ``&&  ||  ;  |  |&  &`` (and recurse into a ``<shell> -c "<script>"``
    wrapper so wrapped commands are judged on their real contents);
  * **strip** leading environment-variable assignments (``FOO=bar cmd``),
    flagging *unsafe* ones (``PATH``, ``LD_PRELOAD`` ...) that can change which
    binary runs — those poison any prefix we might extract; and
  * **extract** a stable command prefix (``git commit -m x`` -> ``git commit``)
    so an approval can be remembered as a prefix rule instead of the exact
    string, the way Claude Code's ``getSimpleCommandPrefix`` works.

Everything degrades gracefully: an unparseable line (unbalanced quotes, ...)
yields ``None`` from the structured helpers and ``[command]`` from
:func:`segment_strings`, so callers always have something to fall back on.
"""
from __future__ import annotations

import shlex
from typing import Optional

# Shell operators that separate independent commands on one line.
_SEPARATORS: frozenset[str] = frozenset({"&&", "||", ";", "|", "|&", "&"})

# Shells we will peek inside for a ``-c``/``-lc`` script argument.
_SHELLS: frozenset[str] = frozenset({"sh", "bash", "zsh", "dash", "ksh"})

# Environment variables that are safe to skip when extracting a command prefix:
# they tweak behavior but never change *which* binary runs.
_SAFE_ENV_VARS: frozenset[str] = frozenset(
    {
        "NODE_ENV",
        "PYTHONUNBUFFERED",
        "PYTHONDONTWRITEBYTECODE",
        "RUST_BACKTRACE",
        "RUST_LOG",
        "LANG",
        "LC_ALL",
        "LANGUAGE",
        "TZ",
        "TERM",
        "CI",
        "DEBUG",
        "FORCE_COLOR",
        "NO_COLOR",
        "GIT_PAGER",
        "PAGER",
        "EDITOR",
    }
)

# Environment variables that DO change which binary runs / how it is linked.
# Their presence means we cannot vouch for a stable prefix — refuse to extract.
_UNSAFE_ENV_VARS: frozenset[str] = frozenset(
    {
        "PATH",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "PYTHONPATH",
        "NODE_OPTIONS",
        "BASH_ENV",
        "ENV",
        "IFS",
        "SHELL",
    }
)

# Commands that take a meaningful first sub-command worth folding into the
# prefix (``git commit``, ``npm install`` ...). For everything else the prefix
# is just the command name.
_SUBCOMMAND_COMMANDS: frozenset[str] = frozenset(
    {
        "git",
        "npm",
        "pnpm",
        "yarn",
        "docker",
        "docker-compose",
        "cargo",
        "pip",
        "pip3",
        "go",
        "kubectl",
        "apt",
        "apt-get",
        "brew",
        "gh",
        "poetry",
        "uv",
        "make",
        "conda",
        "dotnet",
        "bundle",
        "gem",
        "terraform",
        "systemctl",
    }
)


def _basename(path: str) -> str:
    """Last path component, so ``/usr/bin/ls`` reads as ``ls``."""
    return path.rsplit("/", 1)[-1]


def _tokenize(text: str) -> Optional[list[str]]:
    """POSIX-tokenise a command line, or ``None`` on unbalanced quotes."""
    try:
        return shlex.split(text, comments=False, posix=True)
    except ValueError:
        return None


def parse_segments(command: str) -> Optional[list[list[str]]]:
    """Split a command line into per-command argv lists on shell separators.

    Returns ``None`` when the line cannot be tokenised. A ``<shell> -c
    "<script>"`` wrapper is expanded into the inner script's segments so the
    real commands are surfaced.
    """
    tokens = _tokenize(command or "")
    if tokens is None:
        return None
    if not tokens:
        return []

    segments: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in _SEPARATORS:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(tok)
    if current:
        segments.append(current)

    # Expand any shell -c "<script>" wrapper into the inner segments.
    expanded: list[list[str]] = []
    for argv in segments:
        inner = _unwrap_shell_c(argv)
        if inner is None:
            expanded.append(argv)
        else:
            sub = parse_segments(inner)
            if sub is None:
                return None
            expanded.extend(sub)
    return expanded


def _unwrap_shell_c(argv: list[str]) -> Optional[str]:
    """If ``argv`` is ``<shell> [-flags] -c <script>``, return the script string."""
    if not argv or _basename(argv[0]) not in _SHELLS:
        return None
    for i, tok in enumerate(argv[1:], start=1):
        # -c, -lc, -ic etc. — any flag bundle ending in 'c'.
        if tok.startswith("-") and tok.endswith("c") and i + 1 < len(argv):
            return argv[i + 1]
    return None


def segment_strings(command: str) -> list[str]:
    """Split ``command`` into segment strings for per-segment rule matching.

    Each segment is the normalised (re-quoted) form of one command between
    operators. An unparseable line falls back to ``[command]`` so the caller
    still evaluates *something* rather than silently dropping the call.
    """
    segments = parse_segments(command)
    if not segments:
        text = (command or "").strip()
        return [text] if text else []
    return [shlex.join(argv) for argv in segments if argv]


def _strip_env(argv: list[str]) -> tuple[list[str], bool]:
    """Drop leading ``VAR=value`` assignments from ``argv``.

    Returns ``(rest, saw_unsafe)`` where ``rest`` is the argv past the leading
    assignments and ``saw_unsafe`` is True if any stripped assignment targeted
    an env var that can change which binary runs (see ``_UNSAFE_ENV_VARS``).
    """
    saw_unsafe = False
    i = 0
    for tok in argv:
        eq = tok.find("=")
        # A leading token is an assignment only if it looks like NAME=... with a
        # valid identifier name and no shell metacharacters before the '='.
        if eq <= 0 or not tok[:eq].replace("_", "").isalnum() or not tok[0].isalpha():
            break
        name = tok[:eq]
        if name in _UNSAFE_ENV_VARS:
            saw_unsafe = True
        i += 1
    return argv[i:], saw_unsafe


def prefix_tokens(command: str) -> Optional[list[str]]:
    """Env-stripped argv of the FIRST segment, or ``None``.

    ``None`` when the line is unparseable or carries an unsafe env assignment
    (we cannot trust the command identity then). Used by prefix-rule matching.
    """
    segments = parse_segments(command)
    if not segments:
        return None
    argv, saw_unsafe = _strip_env(segments[0])
    if saw_unsafe or not argv:
        return None
    return argv


def command_prefix(command: str) -> Optional[str]:
    """Extract a stable command prefix for remembering an approval as a rule.

    ``git commit -m "x"`` -> ``"git commit"``; ``ls -la`` -> ``"ls"``;
    ``npm install foo`` -> ``"npm install"``. Returns ``None`` when the command
    is unparseable or uses an unsafe env assignment (``PATH=... cmd``).

    Only commands in ``_SUBCOMMAND_COMMANDS`` fold their first sub-command into
    the prefix, and only when that token is a bare word (not a flag, path, or
    number) — so we never grant ``git -C /other`` via a ``git`` prefix.
    """
    argv = prefix_tokens(command)
    if not argv:
        return None
    cmd = _basename(argv[0])
    if not cmd:
        return None
    if cmd in _SUBCOMMAND_COMMANDS and len(argv) > 1 and _is_bare_word(argv[1]):
        return f"{cmd} {argv[1]}"
    return cmd


def _is_bare_word(token: str) -> bool:
    """True for a sub-command-like token: letters/digits/_/- , not a flag/path."""
    if not token or token[0] == "-" or "/" in token or "." in token:
        return False
    return token.replace("_", "").replace("-", "").isalnum() and not token.isdigit()
