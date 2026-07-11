#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Route-class post-processing primitives — vendored from opensquilla.

Source: ``squilla_router/models/.../runtime_src/src/router/predictor.py``.

Only the model-free, config-driven post-processing helpers are kept here — the
6 deterministic layers (margin upgrade / R1 rescue / flag overrides / sticky
tier), thinking-mode & prompt-policy derivation, and model selection. The
Phase-3 inference package (:mod:`mote.router.ml.inference`) imports these.

The v1/v2 ``SquillaRouter`` / ``CascadeRouter`` orchestrators + their
``apply_post_processing`` / ``_detect_model_version`` / ``_reconcile_extractor_schema``
helpers are intentionally dropped: they drove the legacy single-LightGBM path via
the training-time ``FeatureExtractor`` (also dropped), which Phase-3 replaces.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
from mote.router.ml.flags import RoutingFlags

ROUTE_CLASSES = ["R0", "R1", "R2", "R3"]
_CLASS_TO_IDX = {c: i for i, c in enumerate(ROUTE_CLASSES)}


@dataclass
class RoutingResult:
    route_class: str
    probabilities: dict
    difficulty_score: float
    margin: float
    flags: RoutingFlags
    tier: str
    thinking_mode: str
    prompt_policy: str
    prompt_hint: str
    selected_model: str
    trajectory: str = "COLD_START"
    model_version: str = "v1"
    # v4 additions (optional, default no-op for v1/v2/v3 callers):
    aux_decision_probs: dict | None = None
    bge_channels_used: list = field(default_factory=list)
    asst_signal_present: bool = False
    aux_downgrade_applied: bool = False
    sticky_applied: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _apply_margin_upgrade(route_class: str, margin: float, config: dict) -> str:
    threshold = config.get("thresholds", {}).get("margin_upgrade", 0.15)
    if margin < threshold:
        idx = _CLASS_TO_IDX[route_class]
        if idx < len(ROUTE_CLASSES) - 1:
            return ROUTE_CLASSES[idx + 1]
    return route_class


def _apply_r1_rescue(route_class: str, probs: np.ndarray, config: dict) -> str:
    """Rescue R1 from R0 only (safe upward direction).

    Only promotes R0→R1 when R1 is a close second. Never demotes R2→R1
    because that would increase under-routing (complex task on weak model).
    """
    rescue = config.get("thresholds", {}).get("r1_rescue", {})
    r0_gap = rescue.get("from_r0_max_gap", 0.20)

    if route_class == "R0":
        r1_prob = float(probs[1])
        r0_prob = float(probs[0])
        if r0_prob - r1_prob < r0_gap:
            return "R1"
    return route_class


def _apply_flag_overrides(route_class: str, flags: RoutingFlags, config: dict) -> str:
    idx = _CLASS_TO_IDX[route_class]
    if flags.high_risk:
        idx = max(idx, _CLASS_TO_IDX["R2"])
    if flags.debug and flags.long_context:
        idx = max(idx, _CLASS_TO_IDX["R2"])
    if flags.repo_arch:
        idx = max(idx, _CLASS_TO_IDX["R1"])
    return ROUTE_CLASSES[idx]


def _derive_thinking_mode(route_class: str, margin: float, flags: RoutingFlags, config: dict) -> str:
    rules = config.get("thinking_mode_rules", {})
    if route_class == "R3":
        return "T3"
    t3_flags = rules.get("T3", {}).get("flags", ["debug", "long_context", "high_risk"])
    if _CLASS_TO_IDX[route_class] >= _CLASS_TO_IDX.get(rules.get("T3", {}).get("min_class", "R2"), 2):
        for flag_name in t3_flags:
            if getattr(flags, flag_name, False):
                return "T3"
    t0_rule = rules.get("T0", {})
    max_class_t0 = t0_rule.get("max_class", "R0")
    if _CLASS_TO_IDX[route_class] <= _CLASS_TO_IDX.get(max_class_t0, 0) and margin >= t0_rule.get("min_margin", 0.5):
        return "T0"
    t1_rule = rules.get("T1", {})
    max_class_t1 = t1_rule.get("max_class", "R1")
    if _CLASS_TO_IDX[route_class] <= _CLASS_TO_IDX.get(max_class_t1, 1) and margin >= t1_rule.get("min_margin", 0.4):
        return "T1"
    return "T2"


def _derive_prompt_policy(difficulty_score: float, margin: float, flags: RoutingFlags, config: dict) -> str:
    policies = config.get("prompt_policies", {})
    p2_conds = policies.get("P2", {}).get("conditions", {})
    any_flags = p2_conds.get("any_flag", ["high_risk", "long_context", "debug", "strict_format"])
    for flag_name in any_flags:
        if getattr(flags, flag_name, False):
            return "P2"
    p0_conds = policies.get("P0", {}).get("conditions", {})
    max_diff = p0_conds.get("max_difficulty", 0.8)
    min_margin = p0_conds.get("min_margin", 0.4)
    no_flags = p0_conds.get("no_flags", ["high_risk", "strict_format", "debug"])
    has_blocking_flag = any(getattr(flags, f, False) for f in no_flags)
    if difficulty_score <= max_diff and margin >= min_margin and not has_blocking_flag:
        return "P0"
    return "P1"


def _select_model(route_class: str, config: dict) -> tuple[str, str]:
    tier_mapping = config.get("tier_mapping", {})
    tier_registry = config.get("tier_registry", {})
    tier = tier_mapping.get(route_class, "M")
    models = tier_registry.get(tier, ["unknown"])
    return tier, models[0]


def _prompt_hint_locale(text: str | None) -> str:
    if not text:
        return "en"
    cjk_count = 0
    latin_count = 0
    for char in text:
        if "\u4e00" <= char <= "\u9fff" or "\u3400" <= char <= "\u4dbf" or "\uf900" <= char <= "\ufaff":
            cjk_count += 1
        elif char.isascii() and char.isalpha():
            latin_count += 1
    if cjk_count >= 2:
        return "zh"
    return "en"


def _get_prompt_hint(policy: str, config: dict, text: str | None = None) -> str:
    policies = config.get("prompt_policies", {})
    p = policies.get(policy, {})
    if _prompt_hint_locale(text) == "zh":
        return p.get("hint_zh", "") or p.get("hint_en", "")
    return p.get("hint_en", "") or p.get("hint_zh", "")


def _apply_sticky_tier(pred_class: str, probs: np.ndarray, history: list | None, cfg: dict) -> str:
    """Layer 6: KV-cache-aware sticky tier.

    When the current prediction is lower than the previous turn's class, stick
    with the previous class to avoid unnecessary KV-cache invalidation from
    tier downgrades.
    """
    if not history or not cfg.get("thresholds", {}).get("kv_cache_aware", False):
        return pred_class
    prev_idx = _CLASS_TO_IDX[history[-1].route_class]
    pred_idx = _CLASS_TO_IDX[pred_class]
    if prev_idx > pred_idx:
        return history[-1].route_class
    return pred_class
