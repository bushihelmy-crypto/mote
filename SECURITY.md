# Security Policy

## Supported versions

Security fixes are applied to the latest released version on the `main` branch.

| Version | Supported |
| ------- | --------- |
| 1.1.x   | ✅        |
| < 1.1   | ❌        |

## Reporting a vulnerability

**Please do not open a public issue for security vulnerabilities.**

Instead, report privately via GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
("Report a vulnerability" under the repository's *Security* tab), or contact a
maintainer directly.

When reporting, please include:

- a description of the issue and its impact,
- steps to reproduce (a minimal proof-of-concept if possible),
- affected version(s) and environment.

We will acknowledge your report, investigate, and keep you updated on the fix.
Please give us a reasonable window to release a fix before any public disclosure.

## Scope notes

mote executes model-driven tool calls (shell, file edits, web browsing).
Its safety model relies on the **permission engine** (approval axis) and the
**sandbox** (path/network axis). Reports that demonstrate a bypass of either —
e.g. command execution escaping the classifier, path escapes, or SSRF past the
network policy — are especially valuable.
