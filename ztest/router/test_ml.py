#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the metagpt.router.ml package (model-free pieces + graceful fallback).

The heavy trained bundle (LightGBM / ONNX / sklearn) is NOT vendored, so these
tests exercise only the deterministic, import-safe layers: trajectory
classification, feature extraction, the config-driven post-processing
primitives, flag computation, runtime-config loading, the verbatim
``apply_postprocess`` pipeline, and the engine's graceful no-bundle fallback.
"""
from __future__ import annotations

import numpy as np
import pytest

from metagpt.router.ml.config import (
    MODEL_BUNDLE_NAME,
    default_model_dir,
    load_runtime_config,
)
from metagpt.router.ml.engine import SquillaMLEngine
from metagpt.router.ml.features import (
    CONTEXT_DIMS,
    HANDCRAFTED_DIMS,
    HIST_DIMS,
    ContextMetadata,
    extract_context_features,
    extract_handcrafted,
    extract_hist_features,
)
from metagpt.router.ml.flags import RoutingFlags, compute_flags
from metagpt.router.ml.inference.postprocess import apply_postprocess
from metagpt.router.ml.inference.types import InferenceRequest
from metagpt.router.ml.predictor import (
    ROUTE_CLASSES,
    _apply_flag_overrides,
    _apply_margin_upgrade,
    _apply_r1_rescue,
    _prompt_hint_locale,
    _select_model,
)
from metagpt.router.ml.trajectory import Trajectory, TurnDecision, classify


@pytest.fixture(scope="module")
def config() -> dict:
    return load_runtime_config()


def _turn(idx, route_class, difficulty=1.0, margin=0.5):
    return TurnDecision(turn_index=idx, route_class=route_class, difficulty=difficulty,
                        margin=margin, top1_label=route_class)


def _flags(**kw) -> RoutingFlags:
    base = dict(high_risk=False, long_context=False, debug=False, repo_arch=False, strict_format=False)
    base.update(kw)
    return RoutingFlags(**base)


class TestTrajectory:
    def test_cold_start_on_empty(self):
        assert classify([]) == Trajectory.COLD_START

    def test_unclear_on_single(self):
        assert classify([_turn(0, "R1")]) == Trajectory.UNCLEAR

    def test_stable_low(self):
        assert classify([_turn(0, "R0"), _turn(1, "R1")]) == Trajectory.STABLE_LOW

    def test_stable_high(self):
        assert classify([_turn(0, "R2"), _turn(1, "R3")]) == Trajectory.STABLE_HIGH

    def test_escalating(self):
        hist = [_turn(0, "R0", 0.0), _turn(1, "R2", 1.0), _turn(2, "R3", 2.0)]
        assert classify(hist) == Trajectory.ESCALATING

    def test_descalating(self):
        hist = [_turn(0, "R3", 3.0), _turn(1, "R1", 2.0), _turn(2, "R0", 1.0)]
        assert classify(hist) == Trajectory.DESCALATING


class TestFeatures:
    def test_handcrafted_dims(self):
        v = extract_handcrafted("hello world")
        assert v.shape == (HANDCRAFTED_DIMS,)
        assert v[0] == len("hello world")
        assert v[1] == 2  # word count

    def test_handcrafted_empty(self):
        v = extract_handcrafted("")
        assert v.shape == (HANDCRAFTED_DIMS,)
        assert v[0] == 0

    def test_context_features_none_is_zeros(self):
        v = extract_context_features(None)
        assert v.shape == (CONTEXT_DIMS,)
        assert not v.any()

    def test_context_features_deep_conversation_flag(self):
        v = extract_context_features(ContextMetadata(turn_index=5))
        assert v[8] == 1.0  # is_deep_conversation (>=4)

    def test_context_features_heavy_context_flag(self):
        v = extract_context_features(ContextMetadata(context_tokens_est=3000))
        assert v[9] == 1.0  # is_heavy_context (>2000)

    def test_hist_features_empty(self):
        v = extract_hist_features([])
        assert v.shape == (HIST_DIMS,)
        assert v[0] == -1  # prev_route_idx sentinel for empty

    def test_hist_features_populated(self):
        v = extract_hist_features([_turn(0, "R2"), _turn(1, "R3")])
        assert v.shape == (HIST_DIMS,)
        assert v[0] == 3  # last route R3 -> idx 3
        assert v[5] == 2  # history length


class TestPredictorPrimitives:
    def test_margin_upgrade_low_margin_promotes(self, config):
        # yaml margin_upgrade threshold = 0.10
        assert _apply_margin_upgrade("R1", 0.05, config) == "R2"

    def test_margin_upgrade_high_margin_keeps(self, config):
        assert _apply_margin_upgrade("R1", 0.9, config) == "R1"

    def test_margin_upgrade_caps_at_r3(self, config):
        assert _apply_margin_upgrade("R3", 0.0, config) == "R3"

    def test_r1_rescue_promotes_close_r0(self, config):
        # from_r0_max_gap = 0.10; gap 0.05 < 0.10 → rescue to R1
        probs = np.array([0.5, 0.45, 0.03, 0.02])
        assert _apply_r1_rescue("R0", probs, config) == "R1"

    def test_r1_rescue_keeps_clear_r0(self, config):
        probs = np.array([0.9, 0.05, 0.03, 0.02])
        assert _apply_r1_rescue("R0", probs, config) == "R0"

    def test_flag_overrides_high_risk_floor_r2(self, config):
        assert _apply_flag_overrides("R0", _flags(high_risk=True), config) == "R2"

    def test_flag_overrides_repo_arch_floor_r1(self, config):
        assert _apply_flag_overrides("R0", _flags(repo_arch=True), config) == "R1"

    def test_flag_overrides_debug_and_long_context_floor_r2(self, config):
        assert _apply_flag_overrides("R0", _flags(debug=True, long_context=True), config) == "R2"

    def test_flag_overrides_never_downgrade(self, config):
        assert _apply_flag_overrides("R3", _flags(repo_arch=True), config) == "R3"

    def test_select_model_maps_tier(self, config):
        tier, model = _select_model("R3", config)
        assert tier == "XL"
        assert "claude" in model

    def test_prompt_hint_locale_zh(self):
        assert _prompt_hint_locale("请帮我重构架构") == "zh"

    def test_prompt_hint_locale_en(self):
        assert _prompt_hint_locale("refactor this code") == "en"

    def test_prompt_hint_locale_none(self):
        assert _prompt_hint_locale(None) == "en"


class TestMLFlags:
    def test_config_driven_high_risk(self, config):
        f = compute_flags("deploy to production and rollback", config)
        assert f.high_risk is True

    def test_config_driven_debug(self, config):
        f = compute_flags("there is an exception traceback", config)
        assert f.debug is True

    def test_context_metadata_forces_long_context(self, config):
        ctx = ContextMetadata(context_tokens_est=5000)
        f = compute_flags("short", config, context=ctx)
        assert f.long_context is True


class TestConfig:
    def test_default_model_dir_name(self):
        assert default_model_dir().name == MODEL_BUNDLE_NAME

    def test_load_runtime_config_has_sections(self, config):
        assert config["route_classes"] == ["R0", "R1", "R2", "R3"]
        assert config["thresholds"]["margin_upgrade"] == 0.10
        assert config["tier_mapping"]["R3"] == "XL"

    def test_load_runtime_config_missing_dir_uses_vendored(self, tmp_path):
        cfg = load_runtime_config(tmp_path / "nonexistent")
        assert "thresholds" in cfg


class TestApplyPostprocess:
    def _req(self, text="hello") -> InferenceRequest:
        return InferenceRequest(
            current_user_text=text,
            history_user_texts=[],
            prev_assistant_text=None,
            prev_assistant_usage=None,
            prev_route_decisions=[],
        )

    def test_bad_shape_raises(self, config):
        with pytest.raises(ValueError):
            apply_postprocess(np.array([0.5, 0.5]), None, self._req(), config)

    def test_argmax_route(self, config):
        # clear R0 winner, trivial ack → collapses to T0/P0
        probs = np.array([0.97, 0.01, 0.01, 0.01])
        decision = apply_postprocess(probs, None, self._req("thanks"), config)
        assert decision.route_class == "R0"
        assert decision.thinking_mode == "T0"
        assert decision.prompt_policy == "P0"

    def test_high_risk_flag_floors_r2(self, config):
        probs = np.array([0.97, 0.01, 0.01, 0.01])
        decision = apply_postprocess(probs, None, self._req("please deploy to production"), config)
        assert decision.route_class in ("R2", "R3")

    def test_under_routing_safety_floor(self, config):
        # R1 argmax but heavy (R2+R3) mass > 0.45 → floored to R2
        probs = np.array([0.1, 0.4, 0.3, 0.2])
        decision = apply_postprocess(probs, None, self._req("normal task"), config)
        assert decision.route_class in ("R2", "R3")


class TestEngineFallback:
    def test_unavailable_when_bundle_missing(self, tmp_path):
        engine = SquillaMLEngine(model_dir=tmp_path / "no-bundle")
        assert engine.available is False

    def test_predict_returns_none_when_unavailable(self, tmp_path):
        engine = SquillaMLEngine(model_dir=tmp_path / "no-bundle")
        req = InferenceRequest(
            current_user_text="x", history_user_texts=[], prev_assistant_text=None,
            prev_assistant_usage=None, prev_route_decisions=[],
        )
        assert engine.predict(req) is None

    def test_config_loaded_even_without_bundle(self, tmp_path):
        engine = SquillaMLEngine(model_dir=tmp_path / "no-bundle")
        assert "thresholds" in engine.config
