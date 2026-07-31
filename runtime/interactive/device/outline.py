"""Backend-agnostic a11y outline — normalized nodes, stable ``@e{N}`` refs, render, diff.

This is the *device-independent* half of the DeviceUse observation contract. A
:class:`DeviceBackend` dumps a raw accessibility tree (uiautomator XML on
Android, an AX tree on desktop, ...) which is normalized into a
:class:`RawOutline` of :class:`RawNode` (rect + role + text + action flags). This
module then:

* assigns each *interactive* node a stable ``@e{N}`` reference in document order,
  valid only for the snapshot it was minted in (:func:`build_snapshot`);
* renders the tree to a compact indented text outline the model reads
  (:func:`render_snapshot`);
* diffs against the previous snapshot so newly-appeared elements are marked with
  a leading ``*`` (handled inside :func:`build_snapshot`).

Keeping ref-stabilization / render / diff here (not in a backend) means a future
desktop or iOS backend reuses all of it and only has to emit :class:`RawNode`s —
exactly the split pi-computer-use uses (its ``view.ts`` stabilizes refs in a
platform-neutral layer). uiautomator XML parsing lives here too
(:func:`parse_uiautomator_xml`) because that XML *is* the de-facto a11y-tree
serialization format; a backend feeds its raw dump through it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional
from xml.etree import ElementTree

# uiautomator ``bounds`` are serialized as ``[x1,y1][x2,y2]``.
_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


@dataclass
class RawNode:
    """One normalized accessibility node — the sole thing a backend emits.

    Backend-independent: ``role`` is a simplified control kind (the widget class'
    last path segment on Android, e.g. ``Button``), ``bounds`` is the on-screen
    rectangle in device pixels, and the boolean flags describe which actions the
    node supports. ``children`` preserves the tree.
    """

    role: str = ""
    text: str = ""
    desc: str = ""  # content-description (a11y label)
    resource_id: str = ""
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)  # x1, y1, x2, y2
    clickable: bool = False
    long_clickable: bool = False
    scrollable: bool = False
    editable: bool = False
    checkable: bool = False
    checked: bool = False
    selected: bool = False
    enabled: bool = True
    focused: bool = False
    children: list["RawNode"] = field(default_factory=list)

    @property
    def center(self) -> tuple[int, int]:
        """Pixel center of the node's bounds (the tap point for a ``@e{N}`` ref)."""
        x1, y1, x2, y2 = self.bounds
        return ((x1 + x2) // 2, (y1 + y2) // 2)

    @property
    def interactive(self) -> bool:
        """Whether this node is actionable (so it earns a ``@e{N}`` reference)."""
        return self.clickable or self.long_clickable or self.scrollable or self.editable

    @property
    def has_content(self) -> bool:
        """Whether this node carries readable text/label worth rendering."""
        return bool(self.text.strip() or self.desc.strip())

    @property
    def signature(self) -> str:
        """Position-independent identity used for the cross-snapshot ``*`` diff.

        Deliberately excludes ``bounds`` so an element that merely scrolls does
        not read as new; role + resource-id + text + label is enough to tell one
        control from another for the "is this new since last snapshot" hint.
        """
        return f"{self.role}|{self.resource_id}|{self.text}|{self.desc}"


@dataclass
class RawOutline:
    """A backend's whole normalized a11y dump: the root node + screen size."""

    root: Optional[RawNode] = None
    width: int = 0
    height: int = 0


@dataclass
class OutlineNode:
    """A :class:`RawNode` decorated with its snapshot ref + is-new flag."""

    raw: RawNode
    ref: str = ""  # "@e5" when interactive, else ""
    is_new: bool = False
    children: list["OutlineNode"] = field(default_factory=list)


@dataclass
class Snapshot:
    """One observation: a stable ``state_id`` + the ref-resolution table + tree.

    ``refs`` maps each minted ``@e{N}`` to the underlying :class:`RawNode`, so an
    action given a ref resolves to a pixel tap point. The ``state_id`` stamps the
    snapshot; a ref is only valid against the snapshot that minted it, which the
    session enforces (a stale ref → re-observe).
    """

    state_id: str
    root: Optional[OutlineNode] = None
    refs: dict[str, RawNode] = field(default_factory=dict)
    width: int = 0
    height: int = 0

    @property
    def empty(self) -> bool:
        """Whether the a11y tree yielded no interactive/readable content.

        An empty outline (a game / custom-drawn / secured surface uiautomator
        cannot see) is the signal to fall back to pure-visual coordinate
        grounding — the screenshot is the robust floor.
        """
        return not self.refs and (self.root is None or not _any_content(self.root))

    def center_of(self, ref: str) -> Optional[tuple[int, int]]:
        """Return the pixel tap point for *ref*, or ``None`` if unknown here."""
        node = self.refs.get(_normalize_ref(ref))
        return node.center if node is not None else None


# ---------------------------------------------------------------------------
# uiautomator XML → RawOutline
# ---------------------------------------------------------------------------


def _parse_bounds(value: str) -> tuple[int, int, int, int]:
    m = _BOUNDS_RE.search(value or "")
    if not m:
        return (0, 0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))


def _simple_role(cls: str) -> str:
    """``android.widget.Button`` → ``Button`` (last dotted segment)."""
    cls = (cls or "").strip()
    return cls.rsplit(".", 1)[-1] if cls else ""


def _bool_attr(el: ElementTree.Element, name: str) -> bool:
    return (el.get(name, "false") or "").strip().lower() == "true"


def _node_from_element(el: ElementTree.Element) -> RawNode:
    cls = el.get("class", "")
    role = _simple_role(cls)
    node = RawNode(
        role=role,
        text=(el.get("text", "") or ""),
        desc=(el.get("content-desc", "") or ""),
        resource_id=(el.get("resource-id", "") or ""),
        bounds=_parse_bounds(el.get("bounds", "")),
        clickable=_bool_attr(el, "clickable"),
        long_clickable=_bool_attr(el, "long-clickable"),
        scrollable=_bool_attr(el, "scrollable"),
        editable=role.endswith("EditText"),
        checkable=_bool_attr(el, "checkable"),
        checked=_bool_attr(el, "checked"),
        selected=_bool_attr(el, "selected"),
        enabled=_bool_attr(el, "enabled"),
        focused=_bool_attr(el, "focused"),
    )
    for child in el.findall("node"):
        node.children.append(_node_from_element(child))
    return node


def parse_uiautomator_xml(xml: str) -> RawOutline:
    """Parse a ``uiautomator dump`` XML string into a :class:`RawOutline`.

    Screen size is derived from the top node's bounds. A malformed / empty dump
    yields an empty outline (never raises) so the caller can fall through to
    pure-visual grounding.
    """
    if not xml or not xml.strip():
        return RawOutline()
    try:
        root_el = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return RawOutline()
    # ``<hierarchy>`` wraps one or more top-level ``<node>`` elements.
    top_nodes = root_el.findall("node")
    if not top_nodes:
        return RawOutline()
    if len(top_nodes) == 1:
        root = _node_from_element(top_nodes[0])
    else:
        # Multiple windows: synthesize a wrapper root holding them all.
        root = RawNode(role="Window")
        for el in top_nodes:
            root.children.append(_node_from_element(el))
    _, _, x2, y2 = root.bounds
    width = x2 if x2 > 0 else max((c.bounds[2] for c in _iter_nodes(root)), default=0)
    height = y2 if y2 > 0 else max((c.bounds[3] for c in _iter_nodes(root)), default=0)
    return RawOutline(root=root, width=width, height=height)


# ---------------------------------------------------------------------------
# Stabilization + diff (build a Snapshot)
# ---------------------------------------------------------------------------


def _iter_nodes(node: RawNode):
    """Depth-first document-order walk over a RawNode tree."""
    yield node
    for child in node.children:
        yield from _iter_nodes(child)


def _any_content(node: OutlineNode) -> bool:
    if node.ref or node.raw.has_content:
        return True
    return any(_any_content(c) for c in node.children)


def _normalize_ref(ref: str) -> str:
    """Accept ``@e5`` / ``e5`` / ``5`` / ``[5]`` → canonical ``@e5``."""
    r = (ref or "").strip().lstrip("[").rstrip("]").strip()
    if r.startswith("@"):
        r = r[1:]
    if r.startswith("e"):
        r = r[1:]
    return f"@e{r}" if r else ""


def build_snapshot(raw: RawOutline, *, state_id: str, prev: Optional[Snapshot] = None) -> Snapshot:
    """Assign ``@e{N}`` refs in document order + mark elements new vs *prev*.

    Interactive nodes get sequential refs (``@e1``, ``@e2``, ...); the ``refs``
    table maps each to its :class:`RawNode` for tap-point resolution. An element
    whose :attr:`RawNode.signature` was absent from *prev* is flagged ``is_new``.
    """
    prev_sigs = set()
    if prev is not None:
        prev_sigs = {n.signature for n in prev.refs.values()}
    counter = [0]
    refs: dict[str, RawNode] = {}

    def visit(rn: RawNode) -> OutlineNode:
        ref = ""
        if rn.interactive:
            counter[0] += 1
            ref = f"@e{counter[0]}"
            refs[ref] = rn
        is_new = bool(prev is not None and rn.interactive and rn.signature not in prev_sigs)
        node = OutlineNode(raw=rn, ref=ref, is_new=is_new)
        node.children = [visit(c) for c in rn.children]
        return node

    root = visit(raw.root) if raw.root is not None else None
    return Snapshot(state_id=state_id, root=root, refs=refs, width=raw.width, height=raw.height)


# ---------------------------------------------------------------------------
# Compact text rendering
# ---------------------------------------------------------------------------


def _render_line(node: OutlineNode, depth: int) -> str:
    rn = node.raw
    parts: list[str] = []
    marker = "*" if node.is_new else ""
    if node.ref:
        parts.append(f"{marker}{node.ref}")
    elif marker:
        parts.append(marker)
    if rn.role:
        parts.append(rn.role)
    if rn.text.strip():
        parts.append(f'"{rn.text.strip()}"')
    if rn.desc.strip() and rn.desc.strip() != rn.text.strip():
        parts.append(f"[{rn.desc.strip()}]")
    flags: list[str] = []
    if rn.editable:
        flags.append("editable")
    if rn.scrollable:
        flags.append("scrollable")
    if rn.checkable:
        flags.append("checked" if rn.checked else "unchecked")
    if rn.selected:
        flags.append("selected")
    if not rn.enabled:
        flags.append("disabled")
    if flags:
        parts.append("(" + ",".join(flags) + ")")
    return "  " * depth + " ".join(parts)


def render_snapshot(snap: Snapshot, *, semantic_only: bool = False) -> str:
    """Render *snap* to a compact indented outline the model reads.

    Only *interesting* nodes (interactive, or carrying text/label) emit a line;
    pure layout containers are skipped but their children keep the hierarchy via
    indentation. ``semantic_only`` is accepted for symmetry with the observe
    modes but does not change the text (the text outline IS the semantic view).
    """
    _ = semantic_only
    if snap.root is None:
        return ""
    lines: list[str] = []

    def walk(node: OutlineNode, depth: int) -> None:
        interesting = bool(node.ref or node.raw.has_content)
        next_depth = depth
        if interesting:
            lines.append(_render_line(node, depth))
            next_depth = depth + 1
        for child in node.children:
            walk(child, next_depth)

    walk(snap.root, 0)
    return "\n".join(lines)


__all__ = [
    "RawNode",
    "RawOutline",
    "OutlineNode",
    "Snapshot",
    "parse_uiautomator_xml",
    "build_snapshot",
    "render_snapshot",
]
