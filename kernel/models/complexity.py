#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Kernel task-complexity analysis for intelligent model routing.

Ported from oh-my-claudecode's ``features/model-routing`` (signals + scorer +
rules engine), trimmed to the part that answers the only question that matters
here: *given a task, which tier of model should run it?*

The pipeline is deterministic and LLM-free:

    prompt text ──► extract signals (lexical / structural / context)
                ──► weighted complexity score
                ──► LOW / MEDIUM / HIGH tier  (a priority rules engine may
                    override the raw score for specific situations)

LOW/MEDIUM/HIGH are pure feature outputs consumed by Product routing policies.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Literal, Optional

ComplexityTier = Literal["LOW", "MEDIUM", "HIGH"]
QuestionDepth = Literal["why", "how", "what", "where", "none"]
Domain = Literal["generic", "frontend", "backend", "infrastructure", "security"]
Reversibility = Literal["easy", "moderate", "difficult"]
ImpactScope = Literal["local", "module", "system-wide"]


# --------------------------------------------------------------------- keywords
COMPLEXITY_KEYWORDS = {
    "architecture": [
        "architecture",
        "refactor",
        "redesign",
        "restructure",
        "reorganize",
        "decouple",
        "modularize",
        "abstract",
        "pattern",
        "design",
    ],
    "debugging": [
        "debug",
        "diagnose",
        "root cause",
        "investigate",
        "trace",
        "analyze",
        "why is",
        "figure out",
        "understand why",
        "not working",
    ],
    "simple": [
        "find",
        "search",
        "locate",
        "list",
        "show",
        "where is",
        "what is",
        "get",
        "fetch",
        "display",
        "print",
    ],
    "risk": [
        "critical",
        "production",
        "urgent",
        "security",
        "breaking",
        "dangerous",
        "irreversible",
        "data loss",
        "migration",
        "deploy",
    ],
}


# ----------------------------------------------------------------- signal types
@dataclass
class LexicalSignals:
    word_count: int = 0
    file_path_count: int = 0
    code_block_count: int = 0
    has_architecture_keywords: bool = False
    has_debugging_keywords: bool = False
    has_simple_keywords: bool = False
    has_risk_keywords: bool = False
    question_depth: QuestionDepth = "none"
    has_implicit_requirements: bool = False


@dataclass
class StructuralSignals:
    estimated_subtasks: int = 1
    cross_file_dependencies: bool = False
    has_test_requirements: bool = False
    domain_specificity: Domain = "generic"
    requires_external_knowledge: bool = False
    reversibility: Reversibility = "easy"
    impact_scope: ImpactScope = "local"


@dataclass
class ContextSignals:
    previous_failures: int = 0
    conversation_turns: int = 0
    plan_complexity: int = 0
    remaining_tasks: int = 0
    agent_chain_depth: int = 0


@dataclass
class ComplexitySignals:
    lexical: LexicalSignals = field(default_factory=LexicalSignals)
    structural: StructuralSignals = field(default_factory=StructuralSignals)
    context: ContextSignals = field(default_factory=ContextSignals)


# ------------------------------------------------------------- lexical helpers
_FILE_PATH_PATTERNS = [
    re.compile(r"(?:^|\s)[.\/~]?(?:[\w-]+\/)+[\w.-]+\.\w+", re.MULTILINE),
    re.compile(r"`[^`]+\.\w+`"),
    re.compile(r"""['\"][^'\"]+\.\w+['\"]"""),
]


def _count_file_paths(prompt: str) -> int:
    count = sum(len(p.findall(prompt)) for p in _FILE_PATH_PATTERNS)
    return min(count, 20)


def _count_code_blocks(prompt: str) -> int:
    fenced = len(re.findall(r"```[\s\S]*?```", prompt))
    indented = len(re.findall(r"(?:^|\n)(?:\s{4}|\t)[^\n]+(?:\n(?:\s{4}|\t)[^\n]+)*", prompt))
    return fenced + indented // 2


def _has_keywords(prompt: str, keywords: list[str]) -> bool:
    return any(kw in prompt for kw in keywords)


def _detect_question_depth(prompt: str) -> QuestionDepth:
    if re.search(r"\bwhy\b.*\?|\bwhy\s+(is|are|does|do|did|would|should|can)", prompt, re.I):
        return "why"
    if re.search(r"\bhow\b.*\?|\bhow\s+(do|does|can|should|would|to)", prompt, re.I):
        return "how"
    if re.search(r"\bwhat\b.*\?|\bwhat\s+(is|are|does|do)", prompt, re.I):
        return "what"
    if re.search(r"\bwhere\b.*\?|\bwhere\s+(is|are|does|do|can)", prompt, re.I):
        return "where"
    return "none"


_VAGUE_PATTERNS = [
    re.compile(r"\bmake it better\b"),
    re.compile(r"\bimprove\b(?!.*(?:by|to|so that))"),
    re.compile(r"\bfix\b(?!.*(?:the|this|that|in|at))"),
    re.compile(r"\boptimize\b(?!.*(?:by|for|to))"),
    re.compile(r"\bclean up\b"),
    re.compile(r"\brefactor\b(?!.*(?:to|by|into))"),
]


def _detect_implicit_requirements(prompt: str) -> bool:
    return any(p.search(prompt) for p in _VAGUE_PATTERNS)


def extract_lexical_signals(prompt: str) -> LexicalSignals:
    lower = prompt.lower()
    words = [w for w in re.split(r"\s+", prompt) if w]
    return LexicalSignals(
        word_count=len(words),
        file_path_count=_count_file_paths(prompt),
        code_block_count=_count_code_blocks(prompt),
        has_architecture_keywords=_has_keywords(lower, COMPLEXITY_KEYWORDS["architecture"]),
        has_debugging_keywords=_has_keywords(lower, COMPLEXITY_KEYWORDS["debugging"]),
        has_simple_keywords=_has_keywords(lower, COMPLEXITY_KEYWORDS["simple"]),
        has_risk_keywords=_has_keywords(lower, COMPLEXITY_KEYWORDS["risk"]),
        question_depth=_detect_question_depth(lower),
        has_implicit_requirements=_detect_implicit_requirements(lower),
    )


# ---------------------------------------------------------- structural helpers
def _estimate_subtasks(prompt: str) -> int:
    count = 1
    count += len(re.findall(r"^[ \t]*[-*•]\s", prompt, re.M))
    count += len(re.findall(r"^[ \t]*\d+[.)]\s", prompt, re.M))
    count += len(re.findall(r"\band\b", prompt, re.I)) // 2
    count += len(re.findall(r"\bthen\b", prompt, re.I))
    return min(count, 10)


_CROSS_FILE_INDICATORS = [
    re.compile(r"multiple files", re.I),
    re.compile(r"across.*files", re.I),
    re.compile(r"several.*files", re.I),
    re.compile(r"all.*files", re.I),
    re.compile(r"throughout.*codebase", re.I),
    re.compile(r"entire.*project", re.I),
    re.compile(r"whole.*system", re.I),
]


def _detect_cross_file_dependencies(prompt: str) -> bool:
    if _count_file_paths(prompt) >= 2:
        return True
    return any(p.search(prompt) for p in _CROSS_FILE_INDICATORS)


_TEST_INDICATORS = [
    re.compile(r"\btests?\b", re.I),
    re.compile(r"\bspec\b", re.I),
    re.compile(r"make sure.*work", re.I),
    re.compile(r"verify", re.I),
    re.compile(r"ensure.*pass", re.I),
    re.compile(r"\bTDD\b"),
    re.compile(r"unit test", re.I),
    re.compile(r"integration test", re.I),
]


def _detect_test_requirements(prompt: str) -> bool:
    return any(p.search(prompt) for p in _TEST_INDICATORS)


_DOMAIN_PATTERNS: dict[str, list[re.Pattern]] = {
    "frontend": [
        re.compile(
            r"\b(react|vue|angular|svelte|css|html|jsx|tsx|component|ui|ux|styling|tailwind|sass|scss)\b",
            re.I,
        ),
        re.compile(r"\b(button|modal|form|input|layout|responsive|animation)\b", re.I),
    ],
    "backend": [
        re.compile(
            r"\b(api|endpoint|database|query|sql|graphql|rest|server|auth|middleware)\b",
            re.I,
        ),
        re.compile(r"\b(node|express|fastify|nest|django|flask|rails)\b", re.I),
    ],
    "infrastructure": [
        re.compile(
            r"\b(docker|kubernetes|k8s|terraform|aws|gcp|azure|ci|cd|deploy|container)\b",
            re.I,
        ),
        re.compile(r"\b(nginx|load.?balancer|scaling|monitoring|logging)\b", re.I),
    ],
    "security": [
        re.compile(
            r"\b(security|auth|oauth|jwt|encryption|vulnerability|xss|csrf|injection)\b",
            re.I,
        ),
        re.compile(r"\b(password|credential|secret|token|permission)\b", re.I),
    ],
}


def _detect_domain(prompt: str) -> Domain:
    for domain, patterns in _DOMAIN_PATTERNS.items():
        if any(p.search(prompt) for p in patterns):
            return domain  # type: ignore[return-value]
    return "generic"


_EXTERNAL_INDICATORS = [
    re.compile(r"\bdocs?\b", re.I),
    re.compile(r"\bdocumentation\b", re.I),
    re.compile(r"\bofficial\b", re.I),
    re.compile(r"\blibrary\b", re.I),
    re.compile(r"\bpackage\b", re.I),
    re.compile(r"\bframework\b", re.I),
    re.compile(r"\bhow does.*work\b", re.I),
    re.compile(r"\bbest practice", re.I),
]


def _detect_external_knowledge(prompt: str) -> bool:
    return any(p.search(prompt) for p in _EXTERNAL_INDICATORS)


_DIFFICULT_INDICATORS = [
    re.compile(r"\bmigrat", re.I),
    re.compile(r"\bproduction\b", re.I),
    re.compile(r"\bdata.*loss", re.I),
    re.compile(r"\bdelete.*all", re.I),
    re.compile(r"\bdrop.*table", re.I),
    re.compile(r"\birreversible", re.I),
    re.compile(r"\bpermanent", re.I),
]
_MODERATE_INDICATORS = [
    re.compile(r"\brefactor", re.I),
    re.compile(r"\brestructure", re.I),
    re.compile(r"\brename.*across", re.I),
    re.compile(r"\bmove.*files", re.I),
    re.compile(r"\bchange.*schema", re.I),
]


def _assess_reversibility(prompt: str) -> Reversibility:
    if any(p.search(prompt) for p in _DIFFICULT_INDICATORS):
        return "difficult"
    if any(p.search(prompt) for p in _MODERATE_INDICATORS):
        return "moderate"
    return "easy"


_SYSTEM_WIDE_INDICATORS = [
    re.compile(r"\bentire\b", re.I),
    re.compile(r"\ball\s+(?:files|components|modules)", re.I),
    re.compile(r"\bwhole\s+(?:project|codebase|system)", re.I),
    re.compile(r"\bsystem.?wide", re.I),
    re.compile(r"\bglobal", re.I),
    re.compile(r"\beverywhere", re.I),
    re.compile(r"\bthroughout", re.I),
]
_MODULE_INDICATORS = [
    re.compile(r"\bmodule", re.I),
    re.compile(r"\bpackage", re.I),
    re.compile(r"\bservice", re.I),
    re.compile(r"\bfeature", re.I),
    re.compile(r"\bcomponent", re.I),
    re.compile(r"\blayer", re.I),
]


def _assess_impact_scope(prompt: str) -> ImpactScope:
    if any(p.search(prompt) for p in _SYSTEM_WIDE_INDICATORS):
        return "system-wide"
    if _count_file_paths(prompt) >= 3:
        return "module"
    if any(p.search(prompt) for p in _MODULE_INDICATORS):
        return "module"
    return "local"


def extract_structural_signals(prompt: str) -> StructuralSignals:
    lower = prompt.lower()
    return StructuralSignals(
        estimated_subtasks=_estimate_subtasks(prompt),
        cross_file_dependencies=_detect_cross_file_dependencies(prompt),
        has_test_requirements=_detect_test_requirements(lower),
        domain_specificity=_detect_domain(lower),
        requires_external_knowledge=_detect_external_knowledge(lower),
        reversibility=_assess_reversibility(lower),
        impact_scope=_assess_impact_scope(prompt),
    )


def extract_all_signals(prompt: str, context: Optional[ContextSignals] = None) -> ComplexitySignals:
    return ComplexitySignals(
        lexical=extract_lexical_signals(prompt),
        structural=extract_structural_signals(prompt),
        context=context or ContextSignals(),
    )


# conversational cues that an earlier attempt failed (→ previous_failures)
_FAILURE_CUES = (
    "error",
    "failed",
    "failure",
    "exception",
    "traceback",
    "doesn't work",
    "does not work",
    "not working",
    "still broken",
    "didn't work",
    "did not work",
    "try again",
    "retry",
)


def signals_from_messages(messages: Optional[list[dict]]) -> ContextSignals:
    """Derive context signals from a full conversation.

    The conversation is what makes "full context" more than a string: it lets
    us count turns and detect that earlier attempts failed — the latter pushes
    routing toward a stronger tier via ``previous_failures``.
    """
    if not messages:
        return ContextSignals()
    turns = 0
    failures = 0
    for m in messages:
        if not isinstance(m, dict):
            continue
        if m.get("role") in ("user", "assistant"):
            turns += 1
        content = str(m.get("content", "")).lower()
        if content and any(cue in content for cue in _FAILURE_CUES):
            failures += 1
    return ContextSignals(conversation_turns=turns, previous_failures=failures)


# ------------------------------------------------------------------- scoring
TIER_THRESHOLDS = {"HIGH": 8, "MEDIUM": 4}

_W_LEXICAL = {
    "word_count_high": 2,
    "word_count_very_high": 1,
    "file_paths_multiple": 1,
    "code_blocks_present": 1,
    "architecture": 3,
    "debugging": 2,
    "simple": -2,
    "risk": 2,
    "question_why": 2,
    "question_how": 1,
    "implicit": 1,
}
_W_STRUCTURAL = {
    "subtasks_many": 3,
    "subtasks_some": 1,
    "cross_file": 2,
    "test": 1,
    "security": 2,
    "infrastructure": 1,
    "external": 1,
    "reversibility_difficult": 2,
    "reversibility_moderate": 1,
    "impact_system_wide": 3,
    "impact_module": 1,
}
_W_CONTEXT = {
    "prev_failure": 2,
    "prev_failure_max": 4,
    "deep_chain": 2,
    "complex_plan": 1,
}


def _score_lexical(s: LexicalSignals) -> int:
    score = 0
    if s.word_count > 200:
        score += _W_LEXICAL["word_count_high"]
        if s.word_count > 500:
            score += _W_LEXICAL["word_count_very_high"]
    if s.file_path_count >= 2:
        score += _W_LEXICAL["file_paths_multiple"]
    if s.code_block_count > 0:
        score += _W_LEXICAL["code_blocks_present"]
    if s.has_architecture_keywords:
        score += _W_LEXICAL["architecture"]
    if s.has_debugging_keywords:
        score += _W_LEXICAL["debugging"]
    if s.has_simple_keywords:
        score += _W_LEXICAL["simple"]
    if s.has_risk_keywords:
        score += _W_LEXICAL["risk"]
    if s.question_depth == "why":
        score += _W_LEXICAL["question_why"]
    elif s.question_depth == "how":
        score += _W_LEXICAL["question_how"]
    if s.has_implicit_requirements:
        score += _W_LEXICAL["implicit"]
    return score


def _score_structural(s: StructuralSignals) -> int:
    score = 0
    if s.estimated_subtasks > 3:
        score += _W_STRUCTURAL["subtasks_many"]
    elif s.estimated_subtasks > 1:
        score += _W_STRUCTURAL["subtasks_some"]
    if s.cross_file_dependencies:
        score += _W_STRUCTURAL["cross_file"]
    if s.has_test_requirements:
        score += _W_STRUCTURAL["test"]
    if s.domain_specificity == "security":
        score += _W_STRUCTURAL["security"]
    elif s.domain_specificity == "infrastructure":
        score += _W_STRUCTURAL["infrastructure"]
    if s.requires_external_knowledge:
        score += _W_STRUCTURAL["external"]
    if s.reversibility == "difficult":
        score += _W_STRUCTURAL["reversibility_difficult"]
    elif s.reversibility == "moderate":
        score += _W_STRUCTURAL["reversibility_moderate"]
    if s.impact_scope == "system-wide":
        score += _W_STRUCTURAL["impact_system_wide"]
    elif s.impact_scope == "module":
        score += _W_STRUCTURAL["impact_module"]
    return score


def _score_context(s: ContextSignals) -> int:
    score = min(s.previous_failures * _W_CONTEXT["prev_failure"], _W_CONTEXT["prev_failure_max"])
    if s.agent_chain_depth >= 3:
        score += _W_CONTEXT["deep_chain"]
    if s.plan_complexity >= 5:
        score += _W_CONTEXT["complex_plan"]
    return score


def complexity_score(signals: ComplexitySignals) -> int:
    return _score_lexical(signals.lexical) + _score_structural(signals.structural) + _score_context(signals.context)


def score_to_tier(score: int) -> ComplexityTier:
    if score >= TIER_THRESHOLDS["HIGH"]:
        return "HIGH"
    if score >= TIER_THRESHOLDS["MEDIUM"]:
        return "MEDIUM"
    return "LOW"


def calculate_confidence(score: int, tier: ComplexityTier) -> float:
    """Higher confidence when the score sits far from a tier boundary (0.5-0.9)."""
    if tier == "LOW":
        min_distance = TIER_THRESHOLDS["MEDIUM"] - score
    elif tier == "HIGH":
        min_distance = score - TIER_THRESHOLDS["HIGH"]
    else:
        min_distance = min(abs(score - TIER_THRESHOLDS["MEDIUM"]), abs(score - TIER_THRESHOLDS["HIGH"]))
    confidence = 0.5 + (min(max(min_distance, 0), 4) / 4) * 0.4
    return round(confidence * 100) / 100


# ------------------------------------------------------------- rules engine
@dataclass
class RoutingRule:
    """A priority-ordered situational override on the raw complexity score."""

    name: str
    condition: Callable[[ComplexitySignals], bool]
    tier: ComplexityTier
    reason: str
    priority: int


# Generic, agent-agnostic situational rules (the agent-specific ones from the
# source are intentionally dropped — they belong to the subagent layer).
DEFAULT_ROUTING_RULES: list[RoutingRule] = [
    RoutingRule(
        "architecture-system-wide",
        lambda s: s.lexical.has_architecture_keywords and s.structural.impact_scope == "system-wide",
        "HIGH",
        "Architectural decisions with system-wide impact",
        70,
    ),
    RoutingRule(
        "security-domain",
        lambda s: s.structural.domain_specificity == "security",
        "HIGH",
        "Security-related tasks require careful reasoning",
        70,
    ),
    RoutingRule(
        "difficult-reversibility-risk",
        lambda s: s.structural.reversibility == "difficult" and s.lexical.has_risk_keywords,
        "HIGH",
        "High-risk, difficult-to-reverse changes",
        70,
    ),
    RoutingRule(
        "deep-debugging",
        lambda s: s.lexical.has_debugging_keywords and s.lexical.question_depth == "why",
        "HIGH",
        "Root cause analysis requires deep reasoning",
        65,
    ),
    RoutingRule(
        "complex-multi-step",
        lambda s: s.structural.estimated_subtasks > 5 and s.structural.cross_file_dependencies,
        "HIGH",
        "Complex multi-step task with cross-file changes",
        60,
    ),
    RoutingRule(
        "simple-search-query",
        lambda s: (
            s.lexical.has_simple_keywords
            and s.structural.estimated_subtasks <= 1
            and s.structural.impact_scope == "local"
            and not s.lexical.has_architecture_keywords
            and not s.lexical.has_debugging_keywords
        ),
        "LOW",
        "Simple search or lookup task",
        60,
    ),
    RoutingRule(
        "short-local-change",
        lambda s: (
            s.lexical.word_count < 50
            and s.structural.impact_scope == "local"
            and s.structural.reversibility == "easy"
            and not s.lexical.has_risk_keywords
        ),
        "LOW",
        "Short, local, easily reversible change",
        55,
    ),
    RoutingRule(
        "moderate-complexity",
        lambda s: 1 < s.structural.estimated_subtasks <= 5,
        "MEDIUM",
        "Moderate complexity with multiple subtasks",
        50,
    ),
    RoutingRule(
        "module-level-work",
        lambda s: s.structural.impact_scope == "module",
        "MEDIUM",
        "Module-level changes",
        45,
    ),
]


@dataclass
class TierDecision:
    tier: ComplexityTier
    confidence: float
    reasons: list[str]


def decide_tier(
    prompt: str,
    *,
    context: Optional[ContextSignals] = None,
    rules: Optional[list[RoutingRule]] = None,
    use_rules: bool = True,
) -> TierDecision:
    """Full pipeline: signals → score → tier, with optional rules-engine override.

    The raw weighted score always sets a baseline tier; if ``use_rules`` and a
    higher-priority rule matches, the rule's tier wins (but we never *downgrade*
    below the score-derived tier on a HIGH score — escalation only).
    """
    signals = extract_all_signals(prompt, context)
    score = complexity_score(signals)
    score_tier = score_to_tier(score)
    reasons = [f"complexity score {score} → {score_tier}"]

    tier = score_tier
    if use_rules:
        ranked = sorted(rules or DEFAULT_ROUTING_RULES, key=lambda r: r.priority, reverse=True)
        for rule in ranked:
            if rule.condition(signals):
                # Rules escalate or refine; never drop a HIGH score down to LOW.
                if not (score_tier == "HIGH" and rule.tier == "LOW"):
                    tier = rule.tier
                    reasons.append(f"rule '{rule.name}': {rule.reason}")
                break

    return TierDecision(tier=tier, confidence=calculate_confidence(score, tier), reasons=reasons)
