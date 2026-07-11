#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Pluggable strategies for intelligent routing (routing method #3).

A strategy maps the candidate :class:`ModelCard` set + a :class:`RoutingRequest`
to a :class:`RoutingDecision` (target model, confidence, source). Three strategies
ship: a deterministic rule-based one (default, no extra LLM call), a
complexity-based one (scores the task to pick a model tier), and an
LLM-judge one (asks a model to pick).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from mote.common.logs import logger
from mote.router.complexity import RoutingRule, decide_tier, signals_from_messages
from mote.router.llm.base_llm import BaseLLM
from mote.router.schema import ModelCard, RoutingDecision, RoutingRequest

# token_estimate above this fraction of a card's context_window is "long context"
LONG_CONTEXT_TRIGGER_TOKENS = 32000
# confidence assigned when we fall back to the default model with no strong signal
DEFAULT_CONFIDENCE = 0.5


class RoutingStrategy(ABC):
    """Strategy interface for intelligent routing.

    The interface is async so an LLM-judge strategy fits the same shape; the
    rule-based strategy simply returns without awaiting.
    """

    @abstractmethod
    async def select(
        self,
        candidates: dict[str, ModelCard],
        request: RoutingRequest,
        *,
        default: str,
    ) -> RoutingDecision:
        ...


class RuleBasedStrategy(RoutingStrategy):
    """Deterministic, LLM-free routing rules (borrowed from opensquilla ordering)."""

    async def select(
        self,
        candidates: dict[str, ModelCard],
        request: RoutingRequest,
        *,
        default: str,
    ) -> RoutingDecision:
        cards = list(candidates.values())

        # 1. Vision / PDF requirement -> first vision-capable card.
        if request.requires_vision or request.requires_pdf:
            for card in cards:
                if card.supports_vision:
                    return RoutingDecision(name=card.name, confidence=0.9, source="rule")
            return RoutingDecision(name=default, confidence=DEFAULT_CONFIDENCE, source="rule", fallback=True)

        # 2. Long context -> a card whose window fits, preferring higher tier.
        tokens = request.token_estimate()
        if tokens >= LONG_CONTEXT_TRIGGER_TOKENS or "long_context" in request.flags:
            fitting = [c for c in cards if c.context_window and c.context_window >= tokens]
            if fitting:
                best = max(fitting, key=lambda c: (c.tier, c.context_window or 0))
                return RoutingDecision(name=best.name, confidence=0.8, source="rule")

        # 3. high_risk / debug -> the strongest (highest tier) card.
        if "high_risk" in request.flags or "debug" in request.flags:
            if cards:
                best = max(cards, key=lambda c: c.tier)
                return RoutingDecision(name=best.name, confidence=0.85, source="rule")

        # 4. prefer_cheap -> the cheapest (lowest tier) card.
        if request.prefer_cheap and cards:
            cheapest = min(cards, key=lambda c: c.tier)
            return RoutingDecision(name=cheapest.name, confidence=0.8, source="rule")

        # 5. No strong signal -> default, low confidence.
        return RoutingDecision(name=default, confidence=DEFAULT_CONFIDENCE, source="rule")


# Map the three complexity bands onto a fractional position in the candidate
# tier ladder (0.0 = cheapest card ... 1.0 = strongest card).
_TIER_BAND_POSITION = {"LOW": 0.0, "MEDIUM": 0.5, "HIGH": 1.0}


class ComplexityStrategy(RoutingStrategy):
    """Score the task's complexity, then pick a model of the matching tier.

    This is the "what situation → which model" core ported from
    oh-my-claudecode: a deterministic, LLM-free pipeline that reads the request
    text (lexical/structural/context signals), computes a weighted complexity
    score plus a priority rules-engine override, and lands on a LOW/MEDIUM/HIGH
    band. The band is mapped onto whatever tier ladder the candidate cards
    expose (``ModelCard.tier``): LOW → cheapest, HIGH → strongest, MEDIUM →
    middle. Vision/PDF requirements still take precedence (a capable card is
    required regardless of complexity).
    """

    def __init__(self, *, rules: Optional[list[RoutingRule]] = None, use_rules: bool = True):
        self.rules = rules
        self.use_rules = use_rules

    @staticmethod
    def _pick_by_band(cards: list[ModelCard], band: str) -> ModelCard:
        """Pick the card whose tier best matches the band's position on the ladder."""
        tiers = sorted({c.tier for c in cards})
        position = _TIER_BAND_POSITION[band]
        # target tier value interpolated across the available distinct tiers
        target = tiers[round(position * (len(tiers) - 1))]
        # closest tier to target; ties break toward the higher tier for HIGH, lower otherwise
        prefer_high = band == "HIGH"
        return min(
            cards,
            key=lambda c: (abs(c.tier - target), -c.tier if prefer_high else c.tier),
        )

    async def select(
        self,
        candidates: dict[str, ModelCard],
        request: RoutingRequest,
        *,
        default: str,
    ) -> RoutingDecision:
        cards = list(candidates.values())
        if not cards:
            return RoutingDecision(name=default, confidence=DEFAULT_CONFIDENCE, source="complexity", fallback=True)

        context = signals_from_messages(request.messages)

        # Vision / PDF requirement takes precedence — a capable card is mandatory.
        if request.requires_vision or request.requires_pdf:
            vision_cards = [c for c in cards if c.supports_vision]
            if vision_cards:
                # among vision-capable cards, still respect complexity band
                decision = decide_tier(
                    request.prompt_text(), context=context, rules=self.rules, use_rules=self.use_rules
                )
                pick = self._pick_by_band(vision_cards, decision.tier)
                return RoutingDecision(
                    name=pick.name,
                    confidence=decision.confidence,
                    source="complexity",
                    tier=decision.tier,
                    reasons=["requires vision/pdf"] + decision.reasons,
                )
            return RoutingDecision(
                name=default,
                confidence=DEFAULT_CONFIDENCE,
                source="complexity",
                fallback=True,
                reasons=["requires vision/pdf but no capable card"],
            )

        # Full-conversation signals already derived above (turns / prior failures).
        decision = decide_tier(request.prompt_text(), context=context, rules=self.rules, use_rules=self.use_rules)
        tier = decision.tier
        reasons = list(decision.reasons)

        # Request-level flags escalate/de-escalate the band (cheap overrides everything cheap-ward).
        if request.prefer_cheap:
            tier, reasons = "LOW", reasons + ["prefer_cheap → LOW"]
        elif "high_risk" in request.flags or "debug" in request.flags:
            tier, reasons = "HIGH", reasons + ["high_risk/debug flag → HIGH"]
        elif "long_context" in request.flags or request.token_estimate() >= LONG_CONTEXT_TRIGGER_TOKENS:
            tier, reasons = "HIGH", reasons + ["long context → HIGH"]

        pick = self._pick_by_band(cards, tier)
        return RoutingDecision(
            name=pick.name,
            confidence=decision.confidence,
            source="complexity",
            tier=tier,
            reasons=reasons,
        )


class LLMJudgeStrategy(RoutingStrategy):
    """Ask an LLM to pick among candidates by name.

    The prompt lists each candidate's name + description + tags; the model is
    asked to reply with exactly one name. An unmatched/garbled reply falls back
    to the default (``fallback=True``).
    """

    def __init__(self, llm: BaseLLM):
        self.llm = llm

    def _build_prompt(self, candidates: dict[str, ModelCard], request: RoutingRequest) -> str:
        lines = ["You are a model router. Choose the single best model for the request.", "", "Models:"]
        for card in candidates.values():
            tags = ", ".join(sorted(card.tags)) if card.tags else "-"
            lines.append(f"- {card.name}: {card.description or '(no description)'} [tier={card.tier}, tags={tags}]")
        lines += [
            "",
            "Request signals:",
            f"- estimated_tokens: {request.token_estimate()}",
            f"- requires_vision: {request.requires_vision}",
            f"- requires_pdf: {request.requires_pdf}",
            f"- prefer_cheap: {request.prefer_cheap}",
            f"- flags: {', '.join(sorted(request.flags)) if request.flags else '-'}",
        ]
        prompt = request.prompt_text()
        if prompt:
            lines += ["", f"Request text (truncated): {prompt[:500]}"]
        lines += ["", "Reply with ONLY the chosen model name, nothing else."]
        return "\n".join(lines)

    async def select(
        self,
        candidates: dict[str, ModelCard],
        request: RoutingRequest,
        *,
        default: str,
    ) -> RoutingDecision:
        prompt = self._build_prompt(candidates, request)
        try:
            reply = await self.llm.aask(prompt, stream=False)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"LLMJudgeStrategy aask failed, falling back to default: {e}")
            return RoutingDecision(name=default, confidence=DEFAULT_CONFIDENCE, source="llm_judge", fallback=True)

        chosen = (reply or "").strip()
        # tolerate extra prose: match a candidate name appearing in the reply.
        if chosen in candidates:
            return RoutingDecision(name=chosen, confidence=0.9, source="llm_judge")
        for name in candidates:
            if name in chosen:
                return RoutingDecision(name=name, confidence=0.7, source="llm_judge")
        return RoutingDecision(name=default, confidence=DEFAULT_CONFIDENCE, source="llm_judge", fallback=True)
