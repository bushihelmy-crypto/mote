"""ResourceRegistry: load/unload/get_all + budgeted most-recent-first projection."""
from mote.common.const import RESOURCE_ID, RESOURCE_KIND, RESOURCE_STICKY
from mote.common.resource import (
    POST_COMPACT_MAX_ROUNDS,
    POST_COMPACT_MAX_TOKENS_PER_UNIT,
    POST_COMPACT_PER_KIND_BUDGET,
    POST_COMPACT_TOKEN_BUDGET,
    ResourceRegistry,
)
from mote.common.resource.registry import _project_one
from mote.common.resource.unit import ResourceUnit
from mote.common.schema import ResourceMessage


def test_load_and_contains_and_len():
    r = ResourceRegistry()
    assert len(r) == 0
    r.load(id="a", kind="skill", content="A")
    assert "a" in r
    assert len(r) == 1


def test_load_same_id_replaces_last_write_wins():
    r = ResourceRegistry()
    r.load(id="a", kind="skill", content="OLD")
    r.load(id="a", kind="skill", content="NEW")
    assert len(r) == 1
    assert r.get_all()[0].content == "NEW"


def test_unload_removes_and_reports_presence():
    r = ResourceRegistry()
    r.load(id="a", kind="skill", content="A")
    assert r.unload("a") is True
    assert "a" not in r
    assert r.unload("a") is False  # already gone


def test_get_all_most_recent_first():
    r = ResourceRegistry()
    # inject with explicit invoked_at so ordering is deterministic
    r._units["old"] = ResourceUnit(id="old", kind="skill", content="O", invoked_at=1.0)
    r._units["new"] = ResourceUnit(id="new", kind="skill", content="N", invoked_at=2.0)
    ids = [u.id for u in r.get_all()]
    assert ids == ["new", "old"]


def test_project_produces_resource_messages_with_metadata():
    r = ResourceRegistry()
    r.load(id="simplify", kind="skill", content="BODY")
    msgs = r.project()
    assert len(msgs) == 1
    m = msgs[0]
    assert isinstance(m, ResourceMessage)
    assert m.metadata[RESOURCE_ID] == "simplify"
    assert m.metadata[RESOURCE_KIND] == "skill"
    assert m.metadata[RESOURCE_STICKY] is True
    # header + body both present
    assert "simplify" in m.content
    assert "BODY" in m.content
    assert "preserved across compaction" in m.content


def test_project_skips_non_sticky():
    r = ResourceRegistry()
    r.load(id="a", kind="skill", content="A", sticky=False)
    r.load(id="b", kind="skill", content="B", sticky=True)
    ids = [m.resource_id for m in r.project()]
    assert ids == ["b"]


def test_project_order_is_most_recent_first():
    r = ResourceRegistry()
    r._units["old"] = ResourceUnit(id="old", kind="skill", content="O", invoked_at=1.0)
    r._units["new"] = ResourceUnit(id="new", kind="skill", content="N", invoked_at=2.0)
    ids = [m.resource_id for m in r.project()]
    assert ids == ["new", "old"]


def test_project_truncates_oversized_unit_head_kept():
    r = ResourceRegistry()
    # ~1 token/line; make it comfortably exceed the per-unit cap
    big = "\n".join(f"line{i}" for i in range(POST_COMPACT_MAX_TOKENS_PER_UNIT + 2_000))
    r.load(id="big", kind="skill", content=big)
    m = r.project()[0]
    assert "line0" in m.content  # head kept
    assert "tokens omitted" in m.content


def test_project_drops_units_over_total_budget():
    r = ResourceRegistry()
    # Each unit ~ per-unit cap; the total budget only fits a handful, so older
    # (lower invoked_at) units past the budget are dropped whole.
    per_unit_lines = POST_COMPACT_MAX_TOKENS_PER_UNIT - 100
    n_units = (POST_COMPACT_TOKEN_BUDGET // POST_COMPACT_MAX_TOKENS_PER_UNIT) + 3
    body = "\n".join(f"x{i}" for i in range(per_unit_lines))
    for k in range(n_units):
        r._units[f"u{k}"] = ResourceUnit(id=f"u{k}", kind="skill", content=body, invoked_at=float(k))
    projected = r.project()
    # fewer than everything (budget-limited), and never zero
    assert 0 < len(projected) < n_units
    # kept ones are the most recent (highest invoked_at => largest index)
    kept_ids = {m.resource_id for m in projected}
    assert f"u{n_units - 1}" in kept_ids  # newest kept
    assert "u0" not in kept_ids  # oldest dropped


def test_project_one_header_format():
    unit = ResourceUnit(id="simplify", kind="skill", content="ignored")
    m = _project_one(unit, "BODY")
    assert m.content.startswith("# Skill: simplify (loaded earlier, preserved across compaction)")
    assert m.content.endswith("BODY")


# --- per-kind sub-budget + round-based reap -----------------------------------


def _tokens_line_body(n_tokens: int) -> str:
    """A body of roughly *n_tokens* (~1 token per short line)."""
    return "\n".join(f"tok{i}" for i in range(n_tokens))


def test_per_kind_budget_does_not_starve_other_kinds():
    """A flood of one capped kind (task_result) cannot crowd out a skill body."""
    r = ResourceRegistry()
    sub_cap = POST_COMPACT_PER_KIND_BUDGET["task_result"]
    # Load one skill (oldest) then many big task_results (newer) that together
    # far exceed the task_result sub-cap.
    r._units["skill1"] = ResourceUnit(id="skill1", kind="skill", content="SKILLBODY", invoked_at=0.0)
    body = _tokens_line_body(sub_cap - 200)
    for k in range(5):
        r._units[f"t{k}"] = ResourceUnit(id=f"t{k}", kind="task_result", content=body, invoked_at=float(k + 1))
    projected = r.project()
    kinds = [m.resource_kind for m in projected]
    # The skill still projects despite the task_result flood (no starvation).
    assert "skill" in kinds
    # task_result is bounded by its own sub-cap — not every one gets in.
    assert kinds.count("task_result") < 5


def test_per_kind_over_budget_skips_not_breaks():
    """A task_result over its sub-cap is skipped, but scanning continues to a
    later (older) skill unit — i.e. skip, not break."""
    r = ResourceRegistry()
    sub_cap = POST_COMPACT_PER_KIND_BUDGET["task_result"]
    big = _tokens_line_body(sub_cap + 500)  # single unit already over the sub-cap
    # Newest is the over-cap task_result; older is a small skill.
    r._units["big_task"] = ResourceUnit(id="big_task", kind="task_result", content=big, invoked_at=2.0)
    r._units["skill1"] = ResourceUnit(id="skill1", kind="skill", content="S", invoked_at=1.0)
    ids = [m.resource_id for m in r.project()]
    # The oversized task_result exceeds its per-kind cap after truncation? It is
    # truncated to PER_UNIT first; if still over sub-cap it is skipped. Either
    # way the skill (scanned after) must still appear — proving skip, not break.
    assert "skill1" in ids


def test_round_reap_recycles_task_result_but_never_skill():
    """A task_result unit unloads after exceeding its max-rounds cap; a skill,
    having no cap, projects forever."""
    r = ResourceRegistry()
    cap = POST_COMPACT_MAX_ROUNDS["task_result"]
    r.load(id="t1", kind="task_result", content="RESULT", sticky=True)
    r.load(id="s1", kind="skill", content="SKILL", sticky=True)
    # Project cap+1 times; on the projection that pushes rounds past the cap the
    # task_result is reaped afterward.
    for _ in range(cap + 1):
        r.project()
    assert "t1" not in r  # recycled after exceeding the round cap
    assert "s1" in r  # skill never reaped by rounds
    # Keep projecting: the skill still comes through.
    ids = [m.resource_id for m in r.project()]
    assert ids == ["s1"]


def test_task_result_projection_carries_kind():
    r = ResourceRegistry()
    r.load(id="bg_3", kind="task_result", content="<task-result>…</task-result>", sticky=True)
    (m,) = r.project()
    assert m.resource_kind == "task_result"
    assert m.metadata[RESOURCE_KIND] == "task_result"
    assert m.resource_id == "bg_3"
