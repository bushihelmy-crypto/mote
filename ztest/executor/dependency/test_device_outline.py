"""Tests for the backend-agnostic device outline (parse / stabilize / render / diff)."""
from __future__ import annotations

from mote.runtime.tools.dependency._device.outline import (
    RawNode,
    RawOutline,
    build_snapshot,
    parse_uiautomator_xml,
    render_snapshot,
)

# A small but representative uiautomator dump: a scrollable list holding a
# button, an edit field, and a plain text label, under a frame layout root.
SAMPLE_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" class="android.widget.FrameLayout" package="com.x"
        content-desc="" clickable="false" enabled="true" scrollable="false"
        long-clickable="false" bounds="[0,0][1080,2340]">
    <node index="0" text="" class="android.widget.ScrollView" scrollable="true"
          clickable="false" enabled="true" bounds="[0,100][1080,2000]">
      <node index="0" text="Settings" class="android.widget.TextView"
            clickable="false" enabled="true" bounds="[40,120][400,180]" />
      <node index="1" text="" content-desc="Search" class="android.widget.Button"
            clickable="true" enabled="true" bounds="[900,120][1040,180]" />
      <node index="2" text="" class="android.widget.EditText" clickable="true"
            enabled="true" bounds="[40,200][1040,280]" />
    </node>
  </node>
</hierarchy>"""


def test_parse_extracts_nodes_and_screen_size():
    outline = parse_uiautomator_xml(SAMPLE_XML)
    assert outline.width == 1080
    assert outline.height == 2340
    assert outline.root is not None
    assert outline.root.role == "FrameLayout"


def test_parse_roles_bounds_flags():
    outline = parse_uiautomator_xml(SAMPLE_XML)
    # Flatten
    seen = {}
    stack = [outline.root]
    while stack:
        n = stack.pop()
        if n is None:
            continue
        seen[n.role] = n
        stack.extend(n.children)
    btn = seen["Button"]
    assert btn.clickable is True
    assert btn.desc == "Search"
    assert btn.bounds == (900, 120, 1040, 180)
    assert btn.center == (970, 150)
    assert seen["EditText"].editable is True
    assert seen["ScrollView"].scrollable is True


def test_parse_empty_or_malformed_yields_empty_outline():
    assert parse_uiautomator_xml("").root is None
    assert parse_uiautomator_xml("<not-valid").root is None
    assert parse_uiautomator_xml("<hierarchy></hierarchy>").root is None


def test_build_snapshot_assigns_refs_to_interactive_only():
    outline = parse_uiautomator_xml(SAMPLE_XML)
    snap = build_snapshot(outline, state_id="s1")
    # ScrollView (scrollable), Button (clickable), EditText (clickable+editable)
    # → 3 interactive nodes. The plain TextView gets no ref.
    assert set(snap.refs) == {"@e1", "@e2", "@e3"}
    # Refs are assigned in document order.
    assert snap.refs["@e1"].role == "ScrollView"
    assert snap.refs["@e2"].role == "Button"
    assert snap.refs["@e3"].role == "EditText"


def test_center_of_resolves_ref_variants():
    outline = parse_uiautomator_xml(SAMPLE_XML)
    snap = build_snapshot(outline, state_id="s1")
    assert snap.center_of("@e2") == (970, 150)
    assert snap.center_of("e2") == (970, 150)
    assert snap.center_of("2") == (970, 150)
    assert snap.center_of("[2]") == (970, 150)
    assert snap.center_of("@e99") is None


def test_render_includes_refs_text_and_marks_new():
    outline = parse_uiautomator_xml(SAMPLE_XML)
    snap = build_snapshot(outline, state_id="s1")
    text = render_snapshot(snap)
    assert "@e2" in text
    assert "Button" in text
    assert '"Settings"' in text  # the TextView label renders even without a ref
    assert "[Search]" in text  # content-desc rendered
    # No previous snapshot → nothing marked new.
    assert "*@e" not in text


def test_diff_marks_only_newly_appeared_elements():
    prev = build_snapshot(parse_uiautomator_xml(SAMPLE_XML), state_id="s1")
    # New outline: same button, plus a brand-new "Save" button.
    new_raw = RawOutline(
        root=RawNode(
            role="FrameLayout",
            bounds=(0, 0, 1080, 2340),
            children=[
                RawNode(role="Button", desc="Search", clickable=True, bounds=(900, 120, 1040, 180)),
                RawNode(role="Button", text="Save", clickable=True, bounds=(40, 300, 400, 360)),
            ],
        ),
        width=1080,
        height=2340,
    )
    snap = build_snapshot(new_raw, state_id="s2", prev=prev)
    text = render_snapshot(snap)
    # The Search button existed before → not new; Save is new → marked "*".
    save_ref = next(r for r, n in snap.refs.items() if n.text == "Save")
    assert f"*{save_ref}" in text
    search_ref = next(r for r, n in snap.refs.items() if n.desc == "Search")
    assert f"*{search_ref}" not in text


def test_empty_property():
    empty = build_snapshot(RawOutline(), state_id="s0")
    assert empty.empty is True
    non_empty = build_snapshot(parse_uiautomator_xml(SAMPLE_XML), state_id="s1")
    assert non_empty.empty is False
