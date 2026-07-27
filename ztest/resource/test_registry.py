"""ResourceRegistry: load/unload/get_all + budgeted most-recent-first projection."""
from mote.contracts.constants.messages import RESOURCE_ID, RESOURCE_KIND, RESOURCE_STICKY
from mote.contracts.schema import ResourceMessage
from mote.runtime.context.sanitization import count_tokens
from mote.runtime.resources import (
    POST_COMPACT_MAX_ROUNDS,
    POST_COMPACT_MAX_TOKENS_PER_UNIT,
    POST_COMPACT_PER_KIND_BUDGET,
    POST_COMPACT_TOKEN_BUDGET,
    ResourceRegistry,
)
from mote.runtime.resources.registry import _project_one
from mote.runtime.resources.unit import ResourceUnit


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
    """A body of roughly *n_tokens* (short lines; over-provisions vs the tokenizer
    so it comfortably EXCEEDS a cap of *n_tokens* — used where 'big enough' matters,
    not an exact count)."""
    return "\n".join(f"tok{i}" for i in range(n_tokens))


def _sized_body(target_tokens: int) -> str:
    """A body measured to be just UNDER *target_tokens* by the real tokenizer.

    Grows line-by-line until one more line would cross the target, so callers can
    assert precise budget-boundary behavior without hardcoding the tokenizer's
    (non-1:1) line→token ratio."""
    lines: list[str] = []
    while True:
        candidate = "\n".join(lines + [f"tok{len(lines)}"])
        if count_tokens(candidate) >= target_tokens:
            return "\n".join(lines) if lines else candidate
        lines.append(f"tok{len(lines)}")


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


# --- revealed-tool descriptions ("tool" kind) decoupled from Skill's assumptions -


def test_tool_kind_has_own_sub_budget():
    """A flood of revealed-tool descriptions cannot starve a skill — the ``tool``
    sub-cap bounds it independently of Skill's uncapped behavior."""
    r = ResourceRegistry()
    sub_cap = POST_COMPACT_PER_KIND_BUDGET["tool"]
    # Oldest is a skill; newer are many tool descriptions that together exceed the
    # tool sub-cap.
    r._units["skill1"] = ResourceUnit(id="skill1", kind="skill", content="SKILLBODY", invoked_at=0.0)
    body = _tokens_line_body(sub_cap - 200)
    for k in range(5):
        r._units[f"tool{k}"] = ResourceUnit(id=f"tool{k}", kind="tool", content=body, invoked_at=float(k + 1))
    projected = r.project()
    kinds = [m.resource_kind for m in projected]
    assert "skill" in kinds  # skill survives the tool flood (no starvation)
    assert kinds.count("tool") < 5  # tool bounded by its own sub-cap


def test_tool_kind_never_round_reaped_unlike_task_result():
    """A ``tool`` description is a repeatable capability (skill-like), so it is
    NOT round-reaped — ``projection_rounds`` counts compactions survived, not model
    usage, and there is no consume-unload/reuse-refresh seam, so a round cap would
    evict a still-in-use tool. Only task_result (a one-shot payload) reaps."""
    assert "tool" not in POST_COMPACT_MAX_ROUNDS  # deliberately absent
    r = ResourceRegistry()
    task_cap = POST_COMPACT_MAX_ROUNDS["task_result"]
    r.load(id="tool1", kind="tool", content="TOOLDESC", sticky=True)
    r.load(id="task1", kind="task_result", content="RESULT", sticky=True)
    r.load(id="skill1", kind="skill", content="SKILL", sticky=True)
    # Project well past the task_result cap.
    for _ in range(task_cap + 5):
        r.project()
    assert "task1" not in r  # one-shot task_result recycled by rounds
    assert "tool1" in r  # tool persists like a skill (no round cap)
    assert "skill1" in r


def test_tool_soft_lru_ages_out_but_stays_reprojectable():
    """Old tool descriptions stop projecting once the active set exceeds the
    sub-cap (soft LRU) yet remain in the registry — re-projecting if the active
    set later shrinks. This is the age-out mechanism (NOT reaping)."""
    r = ResourceRegistry()
    sub_cap = POST_COMPACT_PER_KIND_BUDGET["tool"]
    per_unit = POST_COMPACT_MAX_TOKENS_PER_UNIT
    # Each unit fits alone (< per-unit cap so untruncated, < sub-cap so one
    # projects) but two together exceed the sub-cap → the newer wins, the older is
    # skipped this projection. Size ~2/3 of the sub-cap so 2× overflows it.
    body = _sized_body(min(int(sub_cap * 0.6), per_unit - 500))
    assert count_tokens(body) < sub_cap  # one fits
    assert 2 * count_tokens(body) > sub_cap  # two overflow
    r._units["old_tool"] = ResourceUnit(id="old_tool", kind="tool", content=body, invoked_at=1.0)
    r._units["new_tool"] = ResourceUnit(id="new_tool", kind="tool", content=body, invoked_at=2.0)
    ids = {m.resource_id for m in r.project()}
    assert "new_tool" in ids  # most-recent wins the budget
    assert "old_tool" not in ids  # older skipped (over sub-cap)
    assert "old_tool" in r  # but STILL held — not reaped
    # Active set shrinks (newer consumed/unloaded): the old tool projects again.
    r.unload("new_tool")
    ids2 = {m.resource_id for m in r.project()}
    assert "old_tool" in ids2  # re-projectable once room frees up
