#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""SquillaStrategy — the full opensquilla routing pipeline (real ML, graceful fallback).

This is the capstone of the opensquilla port. It reproduces opensquilla's
end-to-end routing decision pipeline and drives it from the **real** Phase-3 ML
inference core (TF-IDF+SVD ⊕ BGE features → LightGBM ⊕ MLP probability ensemble),
falling back to Mote's deterministic heuristic complexity scorer when the
model bundle or its heavy optional deps are missing.

Both paths converge on opensquilla's single authoritative post-processing
pipeline (:func:`mote.product.routing.squilla.ml.inference.postprocess.apply_postprocess`):

    messages ─► InferenceRequest
             ─► [ ML engine.predict()      → fused probs + FinalDecision ]
                [ heuristic score_to_probs  → Gaussian probs → apply_postprocess ]
             ─► caller-flag floor (request.flags)
             ─► finalization:
                  confidence gate → complaint upgrade
                  → anti-downgrade (explicit recent state)
                  → large-context floor
             ─► R0-R3 proposal over an explicit immutable candidate set

All cross-turn state is supplied in ``RoutingSessionState`` and returned as a
typed transition. The policy owns no session-keyed mutable stores.
"""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import numpy as np

from mote.contracts.model.routing import (
    CandidateScore,
    RouteCandidate,
    RoutingDegradedReason,
    RoutingInput,
    RoutingProposal,
    RoutingSessionState,
    RoutingStateTransition,
)
from mote.product.routing.squilla.complexity import (
    TIER_THRESHOLDS,
    complexity_score,
    decide_tier,
    extract_all_signals,
    signals_from_messages,
)
from mote.product.routing.squilla.ml.flags import RoutingFlags as MLRoutingFlags
from mote.product.routing.squilla.ml.inference.postprocess import apply_postprocess as ml_apply_postprocess
from mote.product.routing.squilla.ml.inference.types import FinalDecision, InferenceRequest
from mote.product.routing.squilla.ml.predictor import _CLASS_TO_IDX, ROUTE_CLASSES, _apply_flag_overrides
from mote.product.routing.squilla.ml.runtime import RoutingModelLease, RoutingModelRuntime

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
    # large-context floor (token thresholds)
    large_context_t2_floor_tokens: int = 25_000
    large_context_t3_floor_tokens: int = 80_000
    large_context_t3_context_ratio: float = 0.40


@dataclass
class _PredictResult:
    """The output of the shared prediction segment (pre-finalization).

    ``route_class`` is the post-flag route; the remaining fields carry the
    scorer artifacts into finalization and the proposal.
    """

    route_class: str  # post-caller-flag route class ("floored")
    decision: FinalDecision
    probs: dict
    merged_flags: MLRoutingFlags
    reasons: list
    ml: bool  # True when the ML engine produced the decision (else heuristic)
    model_revision: str


InferenceRunner = Callable[..., Awaitable[_PredictResult]]


async def _run_inference_off_loop(function: Callable[..., _PredictResult], *args: object) -> _PredictResult:
    return await asyncio.to_thread(function, *args)


def detect_complaint(message: str, *, max_chars: int = 160) -> list[str]:
    """Return matched complaint terms (empty when message is long or clean)."""
    text = (message or "").strip()
    if max_chars and max_chars > 0 and len(text) > max_chars:
        return []
    lowered = text.lower()
    return [term for term in _COMPLAINT_TERMS if term in lowered]


def _routing_prompt_text(routing_input: RoutingInput) -> str:
    if routing_input.signals.prompt_text:
        return routing_input.signals.prompt_text
    return "\n".join(message.content for message in routing_input.signals.messages if message.content)


class SquillaStrategy:
    """Product-owned policy implementing the Contracts ``RoutingPolicy`` port."""

    def __init__(
        self,
        runtime: RoutingModelRuntime,
        config: Optional[SquillaConfig] = None,
        *,
        inference_runner: InferenceRunner = _run_inference_off_loop,
    ):
        self.config = config or SquillaConfig()
        self.runtime = runtime
        self._inference_runner = inference_runner

    # ----------------------------------------------------- request building
    def _build_inference_request(
        self,
        routing_input: RoutingInput,
        context_signals,
        *,
        previous_route_decisions: list | None = None,
        previous_assistant_usage: dict | None = None,
    ) -> InferenceRequest:
        """Build an opensquilla request from the versioned routing contract.

        The last user turn is the *current* text; prior user turns form the
        history channel; the most recent assistant turn (if any) feeds the
        continuation / assistant signal channels.
        """
        user_texts: list[str] = []
        assistant_texts: list[str] = []
        for message in routing_input.signals.messages:
            role = message.role
            content = message.content
            if role == "user":
                user_texts.append(content)
            elif role == "assistant":
                assistant_texts.append(content)

        if user_texts:
            current_user_text = user_texts[-1]
            history_user_texts = user_texts[:-1]
        else:
            current_user_text = _routing_prompt_text(routing_input)
            history_user_texts = []
        prev_assistant_text = assistant_texts[-1] if assistant_texts else None

        return InferenceRequest(
            current_user_text=current_user_text,
            history_user_texts=history_user_texts,
            prev_assistant_text=prev_assistant_text,
            prev_assistant_usage=previous_assistant_usage,
            prev_route_decisions=list(previous_route_decisions or ()),
            flags_text_override=None,
            context_metadata={
                "turn_index": context_signals.conversation_turns,
                "context_tokens_est": routing_input.signals.estimated_tokens,
            },
        )

    # ------------------------------------------------------- finalization
    def _finalize(
        self,
        route_class: str,
        confidence: float,
        reasons: list[str],
        *,
        prompt: str,
        estimated_tokens: int,
        previous_route_class: str | None,
        seed_route_class: str | None,
        context_window_tokens: int,
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

        previous = previous_route_class

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
        tokens = estimated_tokens
        window = context_window_tokens
        floor = None
        if tokens >= cfg.large_context_t3_floor_tokens or tokens >= int(window * cfg.large_context_t3_context_ratio):
            floor = "R3"
        elif tokens >= cfg.large_context_t2_floor_tokens:
            floor = "R2"
        if floor and route_index(floor) > route_index(final):
            reasons.append(f"large-context floor ({tokens} tok) → {floor}")
            final = floor

        # 6. seed floor — the spawn-time decision's initial tier (raise-only).
        # A soft floor: it only ever lifts ``final`` up to the seeded tier, never
        # caps it — the ML/complaint/large-context layers above may still escalate
        # past the seed. Placed last (after the confidence gate) so the gate can
        # never pull ``final`` below the seed; like the other floors it is a max,
        # so its position among them is immaterial.
        seed = seed_route_class
        if seed and route_index(seed) > route_index(final):
            reasons.append(f"seed floor → {seed}")
            final = seed

        return final, reasons

    # ------------------------------------------------- prediction segment
    def _predict_route_class(
        self,
        routing_input: RoutingInput,
        generation: RoutingModelLease,
        *,
        previous_route_decisions: list | None = None,
        previous_assistant_usage: dict | None = None,
        allow_ml: bool = True,
    ) -> _PredictResult:
        """Run the pre-finalization prediction segment.

        Builds the inference request, runs the ML engine (or the deterministic
        heuristic fallback), then applies the caller-supplied flag floor. Stops
        at the post-flag route class (``floored``) — finalization
        (confidence gate / complaint / anti-downgrade / floors incl. seed) is the
        caller's job.
        """
        messages = [{"role": message.role, "content": message.content} for message in routing_input.signals.messages]
        prompt = _routing_prompt_text(routing_input)
        context_signals = signals_from_messages(messages)
        infer_req = self._build_inference_request(
            routing_input,
            context_signals,
            previous_route_decisions=previous_route_decisions,
            previous_assistant_usage=previous_assistant_usage,
        )
        reasons: list[str] = []
        runtime_config = generation.runtime_config
        result = generation.predict(infer_req) if allow_ml else None
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
                score_to_probs(
                    score,
                    span=self.config.score_span,
                    sharpness=self.config.score_sharpness,
                ),
                dtype=np.float64,
            )
            decision = ml_apply_postprocess(fused, None, infer_req, runtime_config)
            probs = {cls: float(p) for cls, p in zip(ROUTE_CLASSES, fused)}
            reasons.append(f"heuristic fallback: {decision.route_class} (score={score})")
            if score > raw_score:
                reasons.append(
                    f"rules-engine floor {tier_decision.tier} ({raw_score}→{score}): "
                    f"{'; '.join(tier_decision.reasons)}"
                )

        # Caller-supplied flags can only escalate.
        base_route_class = decision.route_class
        merged_flags = _merge_caller_flags(decision.flags, routing_input.signals.flags)
        floored = _apply_flag_overrides(base_route_class, merged_flags, runtime_config)
        if floored != base_route_class:
            reasons.append(f"request flags → {floored}")

        return _PredictResult(
            route_class=floored,
            decision=decision,
            probs=probs,
            merged_flags=merged_flags,
            reasons=reasons,
            ml=result is not None,
            model_revision=generation.revision,
        )

    def _predict_route_class_bounded(self, routing_input, history, usage):
        with self.runtime.pin() as generation:
            return self._predict_route_class(
                routing_input,
                generation,
                previous_route_decisions=history,
                previous_assistant_usage=usage,
                allow_ml=generation.ml_admitted,
            )

    policy_id = "squilla"
    policy_revision = "squilla-policy-v1"

    async def propose(
        self,
        routing_input: RoutingInput,
        candidates: tuple[RouteCandidate, ...],
        state: RoutingSessionState,
    ) -> RoutingProposal:
        """Evaluate Squilla against explicit, recoverable session state."""

        previous = next(
            (item.final_class for item in reversed(state.recent_decisions) if item.final_class in ROUTE_CLASSES),
            None,
        )
        history = [
            {"route_class": item.final_class} for item in state.recent_decisions if item.final_class in ROUTE_CLASSES
        ]
        predicted = await self._inference_runner(
            self._predict_route_class_bounded,
            routing_input,
            history,
            routing_input.signals.previous_assistant_usage,
        )
        confidence = max(predicted.probs.values()) if predicted.probs else 0.5
        final_class, reasons = self._finalize(
            predicted.route_class,
            confidence,
            list(predicted.reasons),
            prompt=_routing_prompt_text(routing_input),
            estimated_tokens=routing_input.signals.estimated_tokens,
            previous_route_class=previous,
            seed_route_class=(state.seed_floor.route_class if state.seed_floor is not None else None),
            context_window_tokens=max(
                (candidate.context_tokens for candidate in candidates),
                default=_DEFAULT_CONTEXT_WINDOW_TOKENS,
            ),
        )
        selected = next(
            (candidate for candidate in candidates if candidate.quality_class == final_class),
            candidates[0],
        )
        scores = tuple(
            CandidateScore(
                route_id=candidate.route_id,
                score=float(predicted.probs.get(candidate.quality_class, 0.0)),
            )
            for candidate in candidates
        )
        return RoutingProposal(
            selected_route_id=selected.route_id,
            policy_id=self.policy_id,
            policy_revision=f"{self.policy_revision}@{predicted.model_revision}",
            feature_schema_revision="squilla-v4",
            base_class=predicted.decision.route_class,
            final_class=final_class,
            base_confidence=confidence,
            scores=scores,
            reason_codes=(
                "ml_score" if predicted.ml else "heuristic_fallback",
                "squilla_finalization",
            ),
            explanation="; ".join(reasons),
            selection_kind="score" if predicted.ml else "heuristic",
            degraded_reason=(None if predicted.ml else RoutingDegradedReason.ML_UNAVAILABLE),
            state_transition=RoutingStateTransition(
                append_final_class=final_class,
                consume_seed=state.seed_floor is not None,
            ),
        )
