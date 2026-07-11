#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for CostNode — the fleet cost mirror tree."""

from mote.router.cost import CostNode, CostTracker, TokenUsage, format_cost_tree


def _node(path, agent_id=None, parent=None):
    node = CostNode(tracker=CostTracker(), parent=parent, agent_path=path, agent_id=agent_id or path)
    if parent is not None:
        parent.children.append(node)
    return node


def _record(node, total=1500, model="gpt-4o"):
    node.tracker.add(TokenUsage(input_tokens=total - 500, output_tokens=500, total_tokens=total), model)


def test_single_node_self_equals_subtree():
    root = _node("/root")
    _record(root)
    assert root.subtree_cost() == root.tracker.total_cost
    assert root.subtree_usage().total_tokens == 1500


def test_parent_child_subtree_cost_accumulates():
    root = _node("/root")
    child = _node("/root/worker", parent=root)
    _record(root, total=1000)
    _record(child, total=2000)
    assert root.tracker.total_cost > 0
    assert child.tracker.total_cost > 0
    assert root.subtree_cost() == root.tracker.total_cost + child.tracker.total_cost
    assert root.subtree_usage().total_tokens == 3000


def test_multi_level_rollup():
    root = _node("/root")
    a = _node("/root/a", parent=root)
    b = _node("/root/a/b", parent=a)
    _record(root)
    _record(a)
    _record(b)
    expected = root.tracker.total_cost + a.tracker.total_cost + b.tracker.total_cost
    assert root.subtree_cost() == expected
    assert root.subtree_usage().total_tokens == 4500
    # the leaf's own subtree is just itself
    assert b.subtree_cost() == b.tracker.total_cost


def test_estimated_flag_rolls_up():
    root = _node("/root")
    child = _node("/root/worker", parent=root)
    _record(child, model="totally-made-up-model-xyz")
    assert child.tracker.has_unknown_model_cost is True
    assert root.tracker.has_unknown_model_cost is False
    assert root.subtree_has_estimated() is True


def test_no_estimated_when_all_known():
    root = _node("/root")
    child = _node("/root/worker", parent=root)
    _record(root)
    _record(child)
    assert root.subtree_has_estimated() is False


def test_walk_preorder():
    root = _node("/root")
    a = _node("/root/a", parent=root)
    _node("/root/a/b", parent=a)
    _node("/root/c", parent=root)
    paths = [n.agent_path for n in root.walk()]
    assert paths == ["/root", "/root/a", "/root/a/b", "/root/c"]


def test_format_cost_tree_renders_each_node_and_total():
    root = _node("/root")
    child = _node("/root/worker", parent=root)
    _record(child)
    out = format_cost_tree(root)
    assert "/root" in out
    assert "/root/worker" in out
    assert "Fleet total" in out
    # the worker line is indented one level
    worker_line = [ln for ln in out.splitlines() if "/root/worker" in ln][0]
    assert worker_line.startswith("  ")
