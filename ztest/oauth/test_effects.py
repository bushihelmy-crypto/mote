from __future__ import annotations

import pytest

from mote.runtime.models.auth.oauth.effects import OAuthEffectKind, OAuthEffectState, OAuthEffectStore
from mote.runtime.models.auth.oauth.storage.base import credential_subject


def test_oauth_effect_intent_and_terminal_evidence_round_trip(tmp_path) -> None:
    store = OAuthEffectStore(tmp_path / "effects.jsonl")
    intent = store.commit_intent(credential_subject("provider"), OAuthEffectKind.REFRESH, 2, 3)
    assert intent.state is OAuthEffectState.INTENT_COMMITTED
    store.settle(intent.effect_id, OAuthEffectState.IN_DOUBT, "transport-result-unknown")
    record = OAuthEffectStore(tmp_path / "effects.jsonl").get(intent.effect_id)
    assert record is not None
    assert record.state is OAuthEffectState.IN_DOUBT
    assert record.evidence_digest.startswith("sha256:")


def test_oauth_effect_identity_conflict_and_terminal_reopen_fail_closed(tmp_path) -> None:
    store = OAuthEffectStore(tmp_path / "effects.jsonl")
    intent = store.commit_intent(credential_subject("provider"), OAuthEffectKind.REVOKE, 2, 3)
    store.settle(intent.effect_id, OAuthEffectState.SUCCEEDED, "revoked")
    with pytest.raises(ValueError):
        store.settle(intent.effect_id, OAuthEffectState.FAILED, "fork")
