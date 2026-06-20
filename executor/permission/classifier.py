"""Command safety classifier — the Phase 3 "intelligent" auto-classifier.

A deterministic, dependency-free port of Codex's two-stage shell pre-check
(``is_known_safe_command`` + ``command_might_be_dangerous``). It turns a raw
shell command string into a :class:`SafetyAssessment` so the permission layer
can:

  * **auto-allow** verifiably read-only commands (``ls``, ``cat``, ``grep``,
    ``git status`` ...) — no prompt in ``default`` mode; and
  * **force an approval** on known-destructive commands (``rm -rf``, ``mkfs``,
    ``sudo ...``) regardless of allow rules / mode (bypass-immune ``ask``).

This is consumed by ``Bash.check_permissions`` (the tool-driven self-check), so
it slots into the existing engine pipeline with no engine changes: a returned
``allow``/``ask`` is honoured at the ``tool_check`` steps. Anything the
classifier cannot positively vouch for is left *unknown* (``known_safe=False``,
``risk="medium"``) and falls through to the normal rules/mode decision.

Conservative by construction: when the command cannot be parsed, redirects to a
file, or uses command substitution, it is treated as *not* known-safe. The goal
is zero false-positives on "safe" (never auto-allow something that writes),
accepting false-negatives (a safe command we don't recognise just prompts).

LLM-based *authorization* assessment (Codex's Guardian "did the user actually
ask for this?" axis) is intentionally out of scope here — it needs model access
and live turn context, and would create a false sense of security if faked. The
``risk`` field is the deterministic signal; an LLM reviewer can layer on later.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from metagpt.executor.permission.command_parse import parse_segments
from metagpt.common.schema.permission_types import RiskLevel

# ---------------------------------------------------------------------------
# Assessment result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SafetyAssessment:
    """Deterministic verdict for a single shell command string.

    * ``known_safe`` — the command is verifiably read-only/benign and may be
      auto-allowed without a prompt.
    * ``risk`` — coarse risk label (``low`` for known-safe, ``high`` for a
      destructive match, ``medium`` for anything unrecognised).
    * ``reason`` — short human-readable rationale (logs / approval prompts).
    """

    known_safe: bool
    risk: RiskLevel
    reason: str = ""


# ---------------------------------------------------------------------------
# Destructive patterns (bypass-immune ask) — consolidated from Bash + Codex
# ---------------------------------------------------------------------------

_DANGEROUS_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\brm\b.*\s-[a-z]*[rf]", re.IGNORECASE),       # rm -rf / rm -f
    re.compile(r":\(\)\s*\{.*\|.*&\s*\}", re.IGNORECASE),       # fork bomb
    re.compile(r"\bmkfs\.", re.IGNORECASE),                     # format filesystem
    re.compile(r"\bdd\b.*\bof=/dev/", re.IGNORECASE),           # raw disk write
    re.compile(r">\s*/dev/sd[a-z]", re.IGNORECASE),             # overwrite block device
    re.compile(r"\b(shutdown|reboot|halt|poweroff)\b", re.IGNORECASE),
    re.compile(r"\bsudo\b", re.IGNORECASE),                     # privilege escalation
    re.compile(r"\bchmod\b.*\b777\b"),                          # world-writable
    re.compile(r"\b(curl|wget)\b.*\|\s*(sudo\s+)?(ba)?sh\b", re.IGNORECASE),  # pipe-to-shell
)

# ---------------------------------------------------------------------------
# Known read-only commands
# ---------------------------------------------------------------------------

# Commands that only read / print and have no file-mutating side effects when
# invoked without output redirection (which is checked separately).
_SAFE_COMMANDS: frozenset[str] = frozenset(
    {
        # file / text reading
        "cat", "head", "tail", "less", "more", "tac", "nl", "fold", "sed",
        "strings", "hexdump", "xxd", "od", "column", "look",
        # listing / paths
        "ls", "pwd", "tree", "basename", "dirname", "realpath", "readlink",
        # searching / filtering
        "grep", "egrep", "fgrep", "rg", "ag", "comm", "cmp", "diff", "find",
        # text transforms (stdout only)
        "wc", "cut", "sort", "uniq", "tr", "rev", "paste", "seq", "expr",
        # info / environment
        "echo", "printf", "whoami", "id", "uname", "hostname", "date", "env",
        "printenv", "stat", "file", "df", "du", "ps", "which", "type", "groups",
        "uptime", "free", "lscpu", "arch", "tty", "locale",
        # checksums (read-only)
        "md5sum", "sha1sum", "sha256sum", "sha512sum", "cksum",
        # trivial no-ops
        "true", "false", "test",
    }
)

# git subcommands that only read repository state.
_SAFE_GIT_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "status", "log", "diff", "show", "describe", "rev-parse", "ls-files",
        "ls-tree", "cat-file", "blame", "shortlog", "reflog", "show-ref",
        "name-rev", "var", "count-objects", "grep", "whatchanged", "rev-list",
        "for-each-ref", "merge-base", "symbolic-ref",
    }
)

# git global flags that change repo/cwd/config context — block (Codex parity).
_GIT_UNSAFE_GLOBAL_FLAGS: frozenset[str] = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--config-env", "--exec-path", "--namespace"}
)

# Flags that turn an otherwise-read tool into a writer.
_FIND_UNSAFE_FLAGS: frozenset[str] = frozenset(
    {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprintf", "-fls"}
)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def classify_command(command: str) -> SafetyAssessment:
    """Classify a raw shell command string into a :class:`SafetyAssessment`."""
    text = (command or "").strip()
    if not text:
        return SafetyAssessment(False, "low", "empty command")

    # 1. Hard destructive signal — wins over everything (bypass-immune ask).
    for pat in _DANGEROUS_PATTERNS:
        if pat.search(text):
            return SafetyAssessment(False, "high", "matches a known-destructive pattern")

    # 2. Output redirection writes a file → cannot be known-safe.
    if _has_write_redirect(text):
        return SafetyAssessment(False, "medium", "redirects output to a file")

    # 3. Command substitution hides an inner command we cannot verify.
    if "$(" in text or "`" in text:
        return SafetyAssessment(False, "medium", "uses command substitution")

    # 4. Split into independent segments; every one must be a known read.
    segments = parse_segments(text)
    if segments is None:
        return SafetyAssessment(False, "medium", "could not parse command")
    for argv in segments:
        if not argv:
            continue
        if not _segment_is_safe(argv):
            return SafetyAssessment(
                False, "medium", f"'{argv[0]}' is not a known read-only command"
            )

    return SafetyAssessment(True, "low", "read-only command")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _has_write_redirect(text: str) -> bool:
    """True if the line redirects output to a file/fd (``>``/``>>``).

    Input redirection (``<``) is reading and does not disqualify on its own.
    """
    # Any '>' that is not part of '2>&1'-style fd duplication still writes a
    # stream somewhere we cannot vouch for, so treat all '>' as a write.
    return ">" in text


def _segment_is_safe(argv: list[str]) -> bool:
    """Whether a single command (argv list) is a known read-only invocation."""
    cmd = _basename(argv[0])
    if cmd == "git":
        return _git_is_safe(argv[1:])
    if cmd not in _SAFE_COMMANDS:
        return False
    if cmd == "find":
        return not any(a in _FIND_UNSAFE_FLAGS for a in argv[1:])
    if cmd == "sed":  # not in safe set, but guard if ever added
        return not any(a in ("-i", "--in-place") or a.startswith("-i") for a in argv[1:])
    return True


def _git_is_safe(rest: list[str]) -> bool:
    """Whether a ``git`` invocation (args after ``git``) only reads state."""
    i = 0
    # Skip/validate leading global flags before the subcommand.
    while i < len(rest) and rest[i].startswith("-"):
        flag = rest[i].split("=", 1)[0]
        if flag in _GIT_UNSAFE_GLOBAL_FLAGS:
            return False
        i += 1
    if i >= len(rest):
        return False  # bare `git` / only flags — nothing to vouch for
    sub = rest[i]
    args = rest[i + 1:]

    if sub == "config":
        # Reads only when an explicit get/list flag is present.
        return any(
            a in ("--get", "--get-all", "--get-regexp", "--list", "-l", "--get-urlmatch")
            for a in args
        )
    if sub in ("branch", "tag", "remote"):
        # List-style is safe; mutating flags are not.
        mutating = {"-d", "-D", "-m", "-M", "--delete", "--move", "--force", "-f",
                    "--add", "--rename", "--set-url", "--prune", "rename", "add",
                    "set-url", "remove", "rm"}
        return not any(a in mutating for a in args)
    return sub in _SAFE_GIT_SUBCOMMANDS


def _basename(path: str) -> str:
    """Last path component, so ``/usr/bin/ls`` matches ``ls``."""
    return path.rsplit("/", 1)[-1]
