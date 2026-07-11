#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""SquillaStrategy — the full opensquilla routing pipeline (real ML, graceful fallback).

This is the capstone of the opensquilla port. It reproduces opensquilla's
end-to-end routing decision pipeline and drives it from the **real** Phase-3 ML
inference core (TF-IDF+SVD ⊕ BGE features → LightGBM ⊕ MLP probability ensemble),
falling back to Mote's deterministic heuristic complexity scorer when the
model bundle or its heavy optional deps are missing.

Both paths converge on opensquilla's single authoritative post-processing
pipeline (:func:`mote.router.ml.inference.postprocess.apply_postprocess`):

    messages ─► InferenceRequest
             ─► [ ML engine.predict()      → fused probs + FinalDecision ]
                [ heuristic score_to_probs  → Gaussian probs → apply_postprocess ]
             ─► caller-flag floor (request.flags)
             ─► finalization:
                  confidence gate → complaint upgrade
                  → anti-downgrade (history, time-windowed)
                  → large-context floor
             ─► R0-R3 → ModelCard (tier ladder)

State (routing history, control holds) is keyed by ``request.session_key``.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np
from mote.router.complexity import (
    TIER_THRESHOLDS,
    complexity_score,
    decide_tier,
    extract_all_signals,
    signals_from_messages,
)
from mote.router.control import RouterControlHoldStore
from mote.router.ml.engine import SquillaMLEngine
from mote.router.ml.flags import RoutingFlags as MLRoutingFlags
from mote.router.ml.inference.postprocess import apply_postprocess as ml_apply_postprocess
from mote.router.ml.inference.types import InferenceRequest
from mote.router.ml.predictor import _CLASS_TO_IDX, ROUTE_CLASSES, _apply_flag_overrides, _get_prompt_hint
from mote.router.schema import ModelCard, RoutingDecision, RoutingRequest
from mote.router.strategy import RoutingStrategy

# default-model token window when no card declares one (opensquilla default)
_DEFAULT_CONTEXT_WINDOW_TOKENS = 200_000

# Rules-engine tier → minimum complexity score (heuristic fallback path only).
# The deterministic rules engine (``decide_tier``) catches situations the raw
# weighted score under-rates — security tasks, system-wide architecture,
# high-risk irreversible changes — by escalating the *tier*. We translate that
# tier into a score floor so the escalation flows through the same
# score→probs→post-processing pipeline.
_TIER_SCORE_FLOOR = {
    "LOW": 0,
    "MEDIUM": TIER_THRESHOLDS["MEDIUM"],
    "HIGH": TIER_THRESHOLDS["HIGH"],
}

_THINKING_MODE_LEVEL = {"T0": None, "T1": "low", "T2": "medium", "T3": "high"}

# multilingual complaint terms (opensquilla engine/steps/squilla_router._COMPLAINT_TERMS)
_COMPLAINT_TERMS = (
    "不对",
    "不行",
    "不对劲",
    "还是不对",
    "完全不对",
    "不是这样",
    "你搞错了",
    "你说错了",
    "回答错了",
    "理解错了",
    "搞错重点了",
    "错了",
    "答非所问",
    "没理解",
    "没听懂",
    "太差",
    "太敷衍",
    "敷衍",
    "没用",
    "废话",
    "离谱",
    "乱说",
    "瞎说",
    "胡扯",
    "答得太差",
    "质量太差",
    "不满意",
    "胡说",
    "漏了",
    "遗漏了",
    "没提到",
    "没覆盖",
    "跑题了",
    "偏题了",
    "不是我要的",
    "没按要求",
    "没有按要求",
    "重写",
    "重新来",
    "重新回答",
    "再来一版",
    "换个说法",
    "重新组织",
    "按我说的重来",
    "你没有回答",
    "垃圾",
    "傻逼",
    "sb",
    "蠢",
    "废物",
    "滚",
    "妈的",
    "操",
    "艹",
    "wrong",
    "incorrect",
    "not correct",
    "you are wrong",
    "completely wrong",
    "totally wrong",
    "not what i asked",
    "you misunderstood",
    "that's not right",
    "this is not right",
    "bad answer",
    "terrible answer",
    "awful answer",
    "horrible answer",
    "poor answer",
    "lazy answer",
    "low quality",
    "poor quality",
    "try again",
    "redo",
    "rewrite",
    "start over",
    "answer again",
    "you missed",
    "missed the point",
    "off topic",
    "irrelevant",
    "not helpful",
    "garbage",
    "trash",
    "crap",
    "sucks",
    "stupid",
    "idiot",
    "moron",
    "dumb",
    "pathetic",
    "ridiculous",
    "fuck",
    "fucking",
    "shit",
    "damn",
    "wtf",
    "asshole",
    "bullshit",
    "nonsense",
    "useless",
)


# --------------------------------------------------- score → probability bridge
def score_to_probs(score: float, *, span: float = 12.0, sharpness: float = 1.2) -> list[float]:
    """Turn a heuristic complexity score into a 4-class probability vector.

    This is the bridge that lets opensquilla's probability-based post-processing
    run on Mote's integer complexity score when the ML engine is unavailable.
    The score is mapped to a continuous *difficulty* ``d`` in ``[0, 3]``
    (``score == span`` → ``d == 3``), then a Gaussian centred on ``d`` produces a
    peaked distribution whose top-2 margin naturally reflects how borderline the
    score is — so the margin upgrade / R1 rescue layers fire on ambiguous scores
    exactly as intended.
    """
    d = max(0.0, min(float(score), span)) / span * 3.0
    weights = [math.exp(-sharpness * (i - d) ** 2) for i in range(4)]
    total = sum(weights) or 1.0
    return [w / total for w in weights]


def route_index(route_class: str) -> int:
    return _CLASS_TO_IDX.get(route_class, 1)


def route_class_for_index(idx: int) -> str:
    idx = max(0, min(idx, len(ROUTE_CLASSES) - 1))
    return ROUTE_CLASSES[idx]


def thinking_mode_to_level(mode: Optional[str]) -> Optional[str]:
    if mode is None:
        return None
    return _THINKING_MODE_LEVEL.get(mode)


def _normalize_decisions(thinking_mode: str, prompt_policy: str) -> tuple[str, str]:
    """Forbid deep thinking (T2/T3) combined with the compress hint (P0)."""
    if thinking_mode in ("T2", "T3") and prompt_policy == "P0":
        return thinking_mode, "P1"
    return thinking_mode, prompt_policy


def _merge_caller_flags(flags_dict: dict, request_flags) -> MLRoutingFlags:
    """Union the post-processed flags with caller-supplied ``request.flags``.

    Explicit caller flags can only escalate (set a flag true), never clear one.
    """
    names = set(request_flags or ())
    return MLRoutingFlags(
        high_risk=bool(flags_dict.get("high_risk")) or "high_risk" in names,
        long_context=bool(flags_dict.get("long_context")) or "long_context" in names,
        debug=bool(flags_dict.get("debug")) or "debug" in names,
        repo_arch=bool(flags_dict.get("repo_arch")) or "repo_arch" in names,
        strict_format=bool(flags_dict.get("strict_format")) or "strict_format" in names,
    )


@dataclass
class SquillaConfig:
    """Tunable knobs for the squilla finalization layer.

    Post-processing thresholds (margin upgrade / R1 rescue / under-routing safety
    / deep-conversation floor) are sourced from ``router.runtime.yaml`` via the
    ML engine config — they are NOT duplicated here.
    """

    # score → probability bridge (heuristic fallback path only)
    score_span: float = 12.0
    score_sharpness: float = 1.2
    # finalization
    confidence_threshold: float = 0.5
    default_route_class: str = "R1"
    complaint_upgrade_enabled: bool = True
    complaint_upgrade_steps: int = 1
    complaint_upgrade_max_chars: int = 160
    kv_cache_anti_downgrade_enabled: bool = True
    kv_cache_anti_downgrade_window_seconds: float = 600.0
    # large-context floor (token thresholds)
    large_context_t2_floor_tokens: int = 25_000
    large_context_t3_floor_tokens: int = 80_000
    large_context_t3_context_ratio: float = 0.40
    # routing-history retention
    max_routing_history: int = 5


@dataclass
class _HistoryEntry:
    final_route_class: str
    ts: float


class RoutingHistoryStore:
    """Per-session routing history, bounded and time-windowed (opensquilla port)."""

    def __init__(self, max_entries: int = 5) -> None:
        self._entries: dict[str, list[_HistoryEntry]] = {}
        self._max = max_entries

    def append(self, session_key: str, route_class: str, *, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        history = self._entries.setdefault(session_key, [])
        history.append(_HistoryEntry(final_route_class=route_class, ts=now))
        if len(history) > self._max:
            self._entries[session_key] = history[-self._max :]

    def previous_within_window(self, session_key: str, *, window: float, now: Optional[float] = None) -> Optional[str]:
        now = time.monotonic() if now is None else now
        history = self._entries.get(session_key)
        if not history:
            return None
        cutoff = now - window
        for entry in reversed(history):
            if entry.ts >= cutoff:
                return entry.final_route_class
        return None

    def clear(self, session_key: Optional[str] = None) -> None:
        if session_key is None:
            self._entries.clear()
        else:
            self._entries.pop(session_key, None)


def detect_complaint(message: str, *, max_chars: int = 160) -> list[str]:
    """Return matched complaint terms (empty when message is long or clean)."""
    text = (message or "").strip()
    if max_chars and max_chars > 0 and len(text) > max_chars:
        return []
    lowered = text.lower()
    return [term for term in _COMPLAINT_TERMS if term in lowered]


class SquillaStrategy(RoutingStrategy):
    """The ported opensquilla pipeline as a pluggable :class:`RoutingStrategy`."""

    source = "squilla"

    def __init__(
        self,
        config: Optional[SquillaConfig] = None,
        *,
        history: Optional[RoutingHistoryStore] = None,
        control_holds: Optional[RouterControlHoldStore] = None,
        engine: Optional[SquillaMLEngine] = None,
        model_dir=None,
    ):
        self.config = config or SquillaConfig()
        self.history = history or RoutingHistoryStore(self.config.max_routing_history)
        self.control_holds = control_holds or RouterControlHoldStore()
        self.engine = engine or SquillaMLEngine(model_dir=model_dir)
        # the runtime config (thresholds / tier mapping / flag rules) backing the
        # post-processing pipeline — present even when the model bundle is absent.
        self.runtime_config = self.engine.config

    # --------------------------------------------------------- card mapping
    @staticmethod
    def _pick_card_for_route(cards: list[ModelCard], route_idx: int) -> ModelCard:
        """Map an R0-R3 route index onto the candidate tier ladder.

        ``route_idx`` (0..3) is positioned proportionally across the distinct
        tiers the candidates expose, then the closest card is chosen (ties break
        toward the higher tier for the strongest route class).
        """
        tiers = sorted({c.tier for c in cards})
        position = route_idx / (len(ROUTE_CLASSES) - 1)
        target = tiers[round(position * (len(tiers) - 1))]
        prefer_high = route_idx >= len(ROUTE_CLASSES) - 1
        return min(
            cards,
            key=lambda c: (abs(c.tier - target), -c.tier if prefer_high else c.tier),
        )

    def _context_window(self, cards: list[ModelCard]) -> int:
        windows = [c.context_window for c in cards if c.context_window]
        return max(windows) if windows else _DEFAULT_CONTEXT_WINDOW_TOKENS

    # ----------------------------------------------------- request building
    def _build_inference_request(self, request: RoutingRequest, context_signals) -> InferenceRequest:
        """Build an opensquilla :class:`InferenceRequest` from a RoutingRequest.

        The last user turn is the *current* text; prior user turns form the
        history channel; the most recent assistant turn (if any) feeds the
        continuation / assistant signal channels.
        """
        user_texts: list[str] = []
        assistant_texts: list[str] = []
        for msg in request.messages or []:
            role = msg.get("role")
            content = msg.get("content") or ""
            if role == "user":
                user_texts.append(content)
            elif role == "assistant":
                assistant_texts.append(content)

        if user_texts:
            current_user_text = user_texts[-1]
            history_user_texts = user_texts[:-1]
        else:
            current_user_text = request.prompt_text()
            history_user_texts = []
        prev_assistant_text = assistant_texts[-1] if assistant_texts else None

        return InferenceRequest(
            current_user_text=current_user_text,
            history_user_texts=history_user_texts,
            prev_assistant_text=prev_assistant_text,
            prev_assistant_usage=None,
            prev_route_decisions=[],
            flags_text_override=None,
            context_metadata={
                "turn_index": context_signals.conversation_turns,
                "context_tokens_est": request.token_estimate(),
            },
        )

    # ------------------------------------------------------- finalization
    def _finalize(
        self,
        route_class: str,
        confidence: float,
        reasons: list[str],
        *,
        request: RoutingRequest,
        cards: list[ModelCard],
        prompt: str,
    ) -> tuple[str, list[str]]:
        cfg = self.config
        final = route_class
        valid = ROUTE_CLASSES

        # 1. confidence gate — low confidence falls back to the default class.
        if confidence < cfg.confidence_threshold and final != cfg.default_route_class:
            reasons.append(
                f"confidence {confidence:.2f} < {cfg.confidence_threshold} " f"→ default {cfg.default_route_class}"
            )
            final = cfg.default_route_class

        now = time.monotonic()
        previous = self.history.previous_within_window(
            request.session_key,
            window=cfg.kv_cache_anti_downgrade_window_seconds,
            now=now,
        )

        # 2. complaint-driven upgrade — user dissatisfaction escalates the tier.
        if cfg.complaint_upgrade_enabled:
            terms = detect_complaint(prompt, max_chars=cfg.complaint_upgrade_max_chars)
            if terms:
                start = final
                if previous and route_index(previous) > route_index(start):
                    start = previous
                upgraded = route_class_for_index(route_index(start) + cfg.complaint_upgrade_steps)
                if upgraded != final:
                    reasons.append(f"complaint {terms[:3]} → upgrade {final}→{upgraded}")
                    final = upgraded

        # 3. KV-cache anti-downgrade — don't drop below a recent stronger tier.
        if cfg.kv_cache_anti_downgrade_enabled and previous in valid and route_index(previous) > route_index(final):
            reasons.append(f"anti-downgrade: hold previous {previous}")
            final = previous

        # 4. large-context floor — big inputs need a roomy/strong tier.
        tokens = request.token_estimate()
        window = self._context_window(cards)
        floor = None
        if tokens >= cfg.large_context_t3_floor_tokens or tokens >= int(window * cfg.large_context_t3_context_ratio):
            floor = "R3"
        elif tokens >= cfg.large_context_t2_floor_tokens:
            floor = "R2"
        if floor and route_index(floor) > route_index(final):
            reasons.append(f"large-context floor ({tokens} tok) → {floor}")
            final = floor

        return final, reasons

    # --------------------------------------------------------- entry point
    async def select(
        self,
        candidates: dict[str, ModelCard],
        request: RoutingRequest,
        *,
        default: str,
    ) -> RoutingDecision:
        cards = list(candidates.values())
        if not cards:
            return RoutingDecision(name=default, confidence=0.5, source="squilla", fallback=True)

        prompt = request.prompt_text()
        context_signals = signals_from_messages(request.messages)

        # 0. router-control hold takes precedence (operator pinned a model).
        hold = self.control_holds.get_valid(request.session_key, decrement=True)
        if hold is not None and hold.name in candidates:
            return RoutingDecision(
                name=hold.name,
                confidence=0.99,
                source="squilla",
                reasons=[f"router-control hold → {hold.name}"],
                extra={"hold": True, "hold_target": hold.target_id},
            )

        # vision / pdf requirement is mandatory — restrict to capable cards.
        pool = cards
        vision_required = request.requires_vision or request.requires_pdf
        if vision_required:
            vision_cards = [c for c in cards if c.supports_vision]
            if not vision_cards:
                return RoutingDecision(
                    name=default,
                    confidence=0.5,
                    source="squilla",
                    fallback=True,
                    reasons=["requires vision/pdf but no capable card"],
                )
            pool = vision_cards

        # 1. ML inference (real LightGBM ⊕ MLP) OR heuristic fallback. Both
        #    converge on opensquilla's authoritative post-processing pipeline.
        infer_req = self._build_inference_request(request, context_signals)
        reasons: list[str] = []
        result = self.engine.predict(infer_req)
        if result is not None:
            decision = result.decision
            probs = dict(result.probabilities)
            reasons.append(f"ml: {decision.route_class} (margin={decision.margin:.2f})")
        else:
            # heuristic fallback: complexity score (floored by the rules engine)
            # → Gaussian 4-class probs → the SAME apply_postprocess pipeline.
            signals = extract_all_signals(prompt, context_signals)
            raw_score = complexity_score(signals)
            tier_decision = decide_tier(prompt, context=context_signals)
            score = max(raw_score, _TIER_SCORE_FLOOR.get(tier_decision.tier, 0))
            fused = np.asarray(
                score_to_probs(score, span=self.config.score_span, sharpness=self.config.score_sharpness),
                dtype=np.float64,
            )
            decision = ml_apply_postprocess(fused, None, infer_req, self.runtime_config)
            probs = {cls: float(p) for cls, p in zip(ROUTE_CLASSES, fused)}
            reasons.append(f"heuristic fallback: {decision.route_class} (score={score})")
            if score > raw_score:
                reasons.append(
                    f"rules-engine floor {tier_decision.tier} ({raw_score}→{score}): "
                    f"{'; '.join(tier_decision.reasons)}"
                )

        # 2. caller-supplied flags floor (request.flags can only escalate).
        base_route_class = decision.route_class
        merged_flags = _merge_caller_flags(decision.flags, request.flags)
        floored = _apply_flag_overrides(base_route_class, merged_flags, self.runtime_config)
        if floored != base_route_class:
            reasons.append(f"request flags → {floored}")

        # 3. finalization (confidence gate / complaint / anti-downgrade / floor).
        confidence = max(probs.values()) if probs else 0.5
        final_class, reasons = self._finalize(floored, confidence, reasons, request=request, cards=pool, prompt=prompt)

        # 4. reconcile thinking/prompt with any tier override, then map to a card.
        thinking_mode, prompt_policy = self._reconcile(
            decision.thinking_mode, decision.prompt_policy, base_route_class, final_class
        )
        pick = self._pick_card_for_route(pool, route_index(final_class))

        # 5. record history for cross-turn anti-downgrade.
        self.history.append(request.session_key, final_class)

        return RoutingDecision(
            name=pick.name,
            confidence=confidence,
            source="squilla",
            tier=final_class,
            reasons=reasons,
            extra={
                "base_route_class": base_route_class,
                "final_route_class": final_class,
                "margin": decision.margin,
                "difficulty": decision.difficulty_score,
                "thinking_mode": thinking_mode,
                "thinking_level": thinking_mode_to_level(thinking_mode),
                "prompt_policy": prompt_policy,
                "prompt_hint": _get_prompt_hint(prompt_policy, self.runtime_config, prompt),
                "flags": vars(merged_flags),
                "probs": probs,
                "ml": result is not None,
                "aux_downgrade_applied": decision.aux_downgrade_applied,
                "sticky_applied": decision.sticky_applied,
            },
        )

    def _reconcile(
        self,
        base_thinking: str,
        base_prompt: str,
        base_route_class: str,
        final_class: str,
    ) -> tuple[str, str]:
        """Promote thinking mode / loosen prompt policy when the tier was raised.

        Mirrors opensquilla's ``_reconcile_controller_with_final_tier``: a final
        class above the post-processed class must not keep a too-shallow thinking
        mode or the compress (P0) hint.
        """
        thinking_mode, prompt_policy = base_thinking, base_prompt
        if final_class == base_route_class:
            return thinking_mode, prompt_policy
        minimum = {"R3": "T3", "R2": "T2", "R1": "T1"}.get(final_class)
        if minimum:
            order = {"T0": 0, "T1": 1, "T2": 2, "T3": 3}
            if order.get(thinking_mode, 0) < order.get(minimum, 0):
                thinking_mode = minimum
        if prompt_policy == "P0" and final_class in ("R2", "R3"):
            prompt_policy = "P1"
        return _normalize_decisions(thinking_mode, prompt_policy)
