#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Product Squilla rule-based routing flags.

Five boolean flags are computed from the request text via keyword/pattern
matching plus a few length heuristics. These flags feed the post-processing
pipeline (``postprocess.apply_flag_overrides`` and the thinking-mode / prompt-
policy derivation). Defaults are lifted verbatim from opensquilla's
``router.runtime.yaml`` (the ``flag_rules`` / ``long_context`` sections); the
ONNX model that produced opensquilla's probabilities is *not* ported — only the
deterministic, config-driven rule layer is.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ----------------------------------------------------------- keyword config
# Lifted from opensquilla router.runtime.yaml `flag_rules`.
HIGH_RISK_KEYWORDS_ZH = ["生产", "部署", "回滚", "迁移", "删除", "客户", "法务", "财务"]
HIGH_RISK_KEYWORDS_EN = [
    "deploy",
    "rollback",
    "migration",
    "delete",
    "overwrite",
    "production",
    "customer-facing",
]
DEBUG_KEYWORDS = [
    "error",
    "bug",
    "exception",
    "traceback",
    "failed",
    "root cause",
    "报错",
    "根因",
    "修复",
]
DEBUG_PATTERNS = [r"Traceback \(most recent", r"stderr:", r"FAILED"]
REPO_ARCH_KEYWORDS = [
    "repo",
    "codebase",
    "monorepo",
    "architecture",
    "重构",
    "架构",
    "module",
    "dependency",
]
STRICT_FORMAT_KEYWORDS = ["JSON", "YAML", "CSV", "schema", "只返回", "不要解释", "按格式"]

# long_context thresholds
LONG_CONTEXT_CHAR_THRESHOLD = 6000
LONG_CONTEXT_CODE_BLOCK_THRESHOLD = 1500
LONG_CONTEXT_LOG_BLOCK_THRESHOLD = 1500
LONG_CONTEXT_FILE_REF_THRESHOLD = 2
# context-metadata enhancement (context_rules.heavy_context_tokens)
HEAVY_CONTEXT_TOKENS = 2000


# --------------------------------------------------------------- detectors
_CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```")
_LOG_BLOCK_RE = re.compile(
    r"(\d{4}[-/]\d{2}[-/]\d{2}[\sT]\d{2}:\d{2}.*\n){3,}" r"|" r"(^\[?(INFO|WARN|ERROR|DEBUG)\]?\s.*\n){3,}",
    re.MULTILINE,
)
_FILE_PATH_RE = re.compile(
    r"(?:^|[\s\"'`(])([a-zA-Z_][\w.-]*/[\w./-]+\.[\w]+)",
    re.MULTILINE,
)


@dataclass
class RoutingFlags:
    """Five boolean routing flags derived from request text."""

    high_risk: bool = False
    long_context: bool = False
    debug: bool = False
    repo_arch: bool = False
    strict_format: bool = False

    def any_of(self, names) -> bool:
        return any(getattr(self, n, False) for n in names)


def _has_keyword(text: str, keywords: list[str]) -> bool:
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in keywords)


def _has_pattern(text: str, patterns: list[str]) -> bool:
    return any(re.search(p, text) for p in patterns)


def _code_block_total_len(text: str) -> int:
    return sum(len(m.group()) for m in _CODE_BLOCK_RE.finditer(text))


def _log_block_total_len(text: str) -> int:
    return sum(len(m.group()) for m in _LOG_BLOCK_RE.finditer(text))


def _file_ref_count(text: str) -> int:
    return len(_FILE_PATH_RE.findall(text))


def compute_flags(text: str, *, context_tokens_est: int = 0) -> RoutingFlags:
    """Compute the five routing flags from ``text``.

    ``context_tokens_est`` is the accumulated-context token estimate; when it
    exceeds ``HEAVY_CONTEXT_TOKENS`` the ``long_context`` flag is forced on even
    if the current message is short (opensquilla's context-metadata enhancement).
    """
    text = text or ""
    high_risk = _has_keyword(text, HIGH_RISK_KEYWORDS_ZH) or _has_keyword(text, HIGH_RISK_KEYWORDS_EN)
    debug = _has_keyword(text, DEBUG_KEYWORDS) or _has_pattern(text, DEBUG_PATTERNS)
    repo_arch = _has_keyword(text, REPO_ARCH_KEYWORDS)
    strict_format = _has_keyword(text, STRICT_FORMAT_KEYWORDS)

    long_context = (
        len(text) >= LONG_CONTEXT_CHAR_THRESHOLD
        or _code_block_total_len(text) >= LONG_CONTEXT_CODE_BLOCK_THRESHOLD
        or _log_block_total_len(text) >= LONG_CONTEXT_LOG_BLOCK_THRESHOLD
        or _file_ref_count(text) >= LONG_CONTEXT_FILE_REF_THRESHOLD
    )
    if context_tokens_est > HEAVY_CONTEXT_TOKENS:
        long_context = True

    return RoutingFlags(
        high_risk=high_risk,
        long_context=long_context,
        debug=debug,
        repo_arch=repo_arch,
        strict_format=strict_format,
    )


def merge_request_flags(flags: RoutingFlags, request_flags) -> RoutingFlags:
    """Union text-derived flags with caller-supplied ``request.flags`` (set[str]).

    Explicit flags can only escalate (set a flag true), never clear one.
    """
    if not request_flags:
        return flags
    names = set(request_flags)
    return RoutingFlags(
        high_risk=flags.high_risk or "high_risk" in names,
        long_context=flags.long_context or "long_context" in names,
        debug=flags.debug or "debug" in names,
        repo_arch=flags.repo_arch or "repo_arch" in names,
        strict_format=flags.strict_format or "strict_format" in names,
    )
