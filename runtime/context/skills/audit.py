"""Skill body supply-chain audit (publish-time 安全底线).

A ``SKILL.md`` *body* is third-party procedural memory: it is injected verbatim
as a prompt (``context: inline``) or run as a child-agent prompt
(``context: fork``). The steady *index* is already scrubbed by
:mod:`mote.runtime.context.sanitization` — the BODY was never screened. This
module audits the body for three publish-time supply-chain risks and returns a
structured :class:`AuditReport` so the loader can refuse a hostile skill
(CRITICAL) or log the rest (WARNING). Pure + stdlib-only (``ast`` + ``re``): no
network, no new deps.

Risks screened:

* ``injection`` — prompt-injection control tokens (reuses the sanitizer's set);
  they have no legitimate place in a skill body → CRITICAL (skill refused).
* ``secret`` — credential-shaped strings that would leak when the body is
  injected → WARNING (docs legitimately show example keys, so never block).
* ``code`` — fenced code blocks with unambiguous malware shapes (``curl … | sh``,
  fork bomb, ``base64 -d | sh``) → CRITICAL; softer dangers (``rm -rf``,
  ``chmod +x``, Python ``eval``/``subprocess`` …) → WARNING.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from mote.runtime.context.sanitization import DANGEROUS_PATTERNS

Category = Literal["injection", "secret", "code"]


class Severity(str, Enum):
    """Audit severity. Only CRITICAL blocks a skill from loading."""

    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True)
class Finding:
    """A single audit hit."""

    category: Category
    severity: Severity
    detail: str
    lineno: int = 0


@dataclass(frozen=True)
class AuditReport:
    """The result of auditing one skill body."""

    findings: tuple[Finding, ...] = ()

    @property
    def ok(self) -> bool:
        """True when nothing CRITICAL was found (safe to register)."""
        return not any(f.severity is Severity.CRITICAL for f in self.findings)

    @property
    def has_findings(self) -> bool:
        return bool(self.findings)

    def summary(self) -> str:
        """One-line, human/log-friendly digest of every finding."""
        return "; ".join(f"[{f.severity.value}] {f.category}@L{f.lineno}: {f.detail}" for f in self.findings)


# --- secret shapes (pattern detection of EMBEDDED unknown credentials) --------
# Distinct from common/secrets/policy.redact (which masks KNOWN values); here we
# detect credential-SHAPED strings a third party may have embedded in the body.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    ("OpenAI-style API key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    (
        "assigned credential literal",
        re.compile(r"""(?i)(?:api[_-]?key|secret|token|password|passwd)\s*[:=]\s*['"][A-Za-z0-9/+_-]{16,}['"]"""),
    ),
)

# --- dangerous shell shapes (screened inside shell/bash code fences) ----------
_SHELL_CRITICAL: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("pipe-to-shell download", re.compile(r"(?:curl|wget)\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba)?sh\b")),
    ("base64 decode piped to shell", re.compile(r"base64\s+(?:-d|--decode)\b[^\n|]*\|\s*(?:ba)?sh\b")),
    ("fork bomb", re.compile(r":\(\)\s*\{\s*:\s*\|\s*:")),
)
_SHELL_WARNING: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("recursive remove", re.compile(r"\brm\s+-[a-zA-Z]*[rR][a-zA-Z]*\b")),
    ("chmod executable", re.compile(r"\bchmod\s+\+x\b")),
    ("write to system path", re.compile(r"(?:>|>>)\s*/(?:etc|bin|sbin|usr|boot)/")),
)

# --- dangerous Python calls (screened inside python code fences via AST) ------
_PY_DANGER_CALLS = frozenset({"eval", "exec", "compile", "__import__"})
_PY_DANGER_ATTRS = frozenset(
    {
        ("os", "system"),
        ("os", "popen"),
        ("subprocess", "run"),
        ("subprocess", "call"),
        ("subprocess", "Popen"),
        ("subprocess", "check_output"),
        ("subprocess", "check_call"),
        ("shutil", "rmtree"),
    }
)

_FENCE_RE = re.compile(r"(?P<fence>```|~~~)(?P<lang>[\w+.-]*)[^\n]*\n(?P<code>.*?)(?P=fence)", re.DOTALL)
_SHELL_LANGS = frozenset({"sh", "bash", "shell", "zsh", "console", "ksh", ""})
_PY_LANGS = frozenset({"python", "py", "python3"})


def _lineno(text: str, pos: int) -> int:
    """1-based line number of character offset ``pos`` in ``text``."""
    return text.count("\n", 0, pos) + 1


def _scan_injection(text: str) -> list[Finding]:
    return [
        Finding("injection", Severity.CRITICAL, f"prompt-injection token {m.group(0)!r}", _lineno(text, m.start()))
        for m in DANGEROUS_PATTERNS.finditer(text)
    ]


def _scan_secrets(text: str) -> list[Finding]:
    findings: list[Finding] = []
    for label, pat in _SECRET_PATTERNS:
        for m in pat.finditer(text):
            findings.append(Finding("secret", Severity.WARNING, f"possible {label}", _lineno(text, m.start())))
    return findings


def _scan_python(code: str, base_line: int) -> list[Finding]:
    """AST-screen one python fence for dangerous calls (call-sites only)."""
    findings: list[Finding] = []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return findings  # a pseudo-code fence isn't real Python — nothing to assert
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        line = base_line + getattr(node, "lineno", 1) - 1
        if isinstance(func, ast.Name) and func.id in _PY_DANGER_CALLS:
            findings.append(Finding("code", Severity.WARNING, f"Python {func.id}() call", line))
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if (func.value.id, func.attr) in _PY_DANGER_ATTRS:
                findings.append(Finding("code", Severity.WARNING, f"Python {func.value.id}.{func.attr}() call", line))
    return findings


def _scan_code(text: str) -> list[Finding]:
    findings: list[Finding] = []
    for m in _FENCE_RE.finditer(text):
        lang = (m.group("lang") or "").lower()
        code = m.group("code")
        base_line = _lineno(text, m.start("code"))
        if lang in _PY_LANGS:
            findings.extend(_scan_python(code, base_line))
        if lang in _SHELL_LANGS:
            for severity, patterns in ((Severity.CRITICAL, _SHELL_CRITICAL), (Severity.WARNING, _SHELL_WARNING)):
                for label, pat in patterns:
                    for hit in pat.finditer(code):
                        findings.append(Finding("code", severity, label, base_line + code.count("\n", 0, hit.start())))
    return findings


def audit_skill_body(instructions: str) -> AuditReport:
    """Screen a skill body for injection / secret / dangerous-code risks.

    Pure and side-effect-free; the loader decides what to do with the report
    (skip on ``not report.ok``, log warnings otherwise).
    """
    if not instructions:
        return AuditReport()
    findings: list[Finding] = []
    findings.extend(_scan_injection(instructions))
    findings.extend(_scan_secrets(instructions))
    findings.extend(_scan_code(instructions))
    return AuditReport(tuple(findings))


__all__ = ["Severity", "Finding", "AuditReport", "audit_skill_body"]
