from ztest.architecture.post_closure_governance import validate


def test_post_closure_governance_is_strict_and_linked() -> None:
    requirement_count, verified_count = validate()
    assert requirement_count == 103
    # The governance recipe bootstraps after all 102 domain recipes pass; its
    # own receipt then promotes the final record without allowing domain gaps.
    assert verified_count >= requirement_count - 1
