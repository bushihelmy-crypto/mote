"""Pure rendering primitives for Product CodeMap context."""

from __future__ import annotations

from typing import Optional

from mote.runtime.context.token_budget import count_tokens
from mote.runtime.context.tokenizer import DEFAULT_TEXT_TOKENIZER
from mote.runtime.file_paths import display_path

MAX_SYMBOLS_PER_FILE = 12
UNREAD_USED_BY_CAP = 8


def render_unread_target(
    path: str,
    unread_symbols: dict,
    unread_module_summary: dict,
    cwd: Optional[str],
    tier: int,
) -> str:
    """Render one unread dependency with optional symbols and purpose."""
    rendered = display_path(path, cwd)
    if tier >= 1:
        return rendered
    names = unread_symbols.get(path) if unread_symbols else None
    if names:
        rendered = f"{rendered} ({', '.join(names)})"
    summary = unread_module_summary.get(path) if unread_module_summary else None
    if summary:
        rendered = f"{rendered} — {summary}"
    return rendered


def render_symbols(head: list, overflow: int, tier: int) -> str:
    """Render a bounded compact symbol list for the selected detail tier."""
    if tier < 1:
        names = [
            f"{symbol.qualified_name}{symbol.signature}" if symbol.signature else symbol.qualified_name
            for symbol in head
        ]
    else:
        names = [symbol.qualified_name for symbol in head]
    rendered = ", ".join(names)
    if overflow > 0:
        rendered = f"{rendered}, (+{overflow} more)"
    return rendered


def render_calls(calls: list) -> str:
    """Aggregate repeated caller-to-callee edges into a compact list."""
    by_caller: dict[str, list[str]] = {}
    seen: set[tuple] = set()
    for call in calls:
        key = (call.caller, call.callee)
        if key in seen:
            continue
        seen.add(key)
        caller = call.caller or "<module>"
        by_caller.setdefault(caller, []).append(call.callee)
    return "; ".join(f"{caller} → {', '.join(callees)}" for caller, callees in by_caller.items())


def render_defines(symbols: list, tier: int) -> list[str]:
    """Render bounded symbol definitions at the selected detail tier."""
    head = symbols[:MAX_SYMBOLS_PER_FILE]
    overflow = len(symbols) - len(head)
    if tier < 1 and any(symbol.summary for symbol in head):
        output = ["    defines:"]
        for symbol in head:
            label = f"{symbol.qualified_name}{symbol.signature or ''}"
            output.append(f"      {label} — {symbol.summary}" if symbol.summary else f"      {label}")
        if overflow > 0:
            output.append(f"      (+{overflow} more)")
        return output
    return [f"    defines: {render_symbols(head, overflow, tier)}"]


def render_unread_used_by(neighborhood, cwd: Optional[str]) -> list[str]:
    """Render bounded reverse dependencies for unread targets."""
    output: list[str] = []
    for target in neighborhood.imports_unread:
        importers = neighborhood.unread_imported_by.get(target) if neighborhood.unread_imported_by else None
        if not importers:
            continue
        head = importers[:UNREAD_USED_BY_CAP]
        names = ", ".join(display_path(path, cwd) for path in head)
        overflow = len(importers) - len(head)
        if overflow > 0:
            names = f"{names} (+{overflow} more)"
        output.append(f"      {display_path(target, cwd)} used by: {names}")
    return output


def render_interface_risk(
    neighborhood,
    cwd: Optional[str],
    changed_names: dict[str, list[str]],
    precise_callers: dict[str, dict[str, list[str]]],
) -> list[str]:
    """Render an interface-change warning and its best available callers."""
    broken_names = changed_names.get(neighborhood.path, [])
    output = [f"    ⚠ interface changed: {', '.join(broken_names)}"]
    precise = precise_callers.get(neighborhood.path) or {}
    rendered_precise = False
    for symbol in broken_names:
        callers = precise.get(symbol)
        if callers:
            names = ", ".join(display_path(path, cwd) for path in callers)
            output.append(f"      {symbol} called by: {names}")
            rendered_precise = True
    if not rendered_precise and neighborhood.imported_by:
        names = ", ".join(display_path(path, cwd) for path in neighborhood.imported_by)
        output.append(f"    used by: {names}")
    return output


def render_surfaced_callers(
    neighborhood,
    cwd: Optional[str],
    surfaced_callers: dict[str, dict[str, list[str]]],
) -> list[str]:
    """Render opportunistic callers for a calm neighborhood."""
    surfaced = surfaced_callers.get(neighborhood.path) or {}
    output: list[str] = []
    for symbol in sorted(surfaced):
        callers = surfaced.get(symbol)
        if callers:
            names = ", ".join(display_path(path, cwd) for path in callers)
            output.append(f"    {symbol} called by: {names}")
    return output


def render_file(
    neighborhood,
    cwd: Optional[str],
    tier: int,
    *,
    in_context: set[str],
    changed_names: dict[str, list[str]],
    precise_callers: dict[str, dict[str, list[str]]],
    surfaced_callers: dict[str, dict[str, list[str]]],
) -> list[str]:
    """Render one neighborhood at the selected degradation tier."""
    interface_changed = neighborhood.path in changed_names
    output = [f"- {display_path(neighborhood.path, cwd)}"]
    body_in_context = neighborhood.path in in_context
    if tier < 1 and neighborhood.module_summary and (not body_in_context or interface_changed):
        output.append(f"    purpose: {neighborhood.module_summary}")
    if neighborhood.symbols and (not body_in_context or interface_changed):
        output.extend(render_defines(neighborhood.symbols, tier))
        if tier < 1:
            calls = render_calls(neighborhood.calls)
            if calls:
                output.append(f"    calls: {calls}")
    if tier < 1 and not interface_changed:
        output.extend(render_surfaced_callers(neighborhood, cwd, surfaced_callers))
    if neighborhood.imports:
        targets = ", ".join(display_path(path, cwd) for path in neighborhood.imports)
        output.append(f"    imports: {targets}")
    if neighborhood.imports_unread:
        unread = ", ".join(
            render_unread_target(
                path,
                neighborhood.unread_symbols,
                neighborhood.unread_module_summary,
                cwd,
                tier,
            )
            for path in neighborhood.imports_unread
        )
        output.append(f"    also imports (unread): {unread}")
        if tier < 1:
            output.extend(render_unread_used_by(neighborhood, cwd))
    if interface_changed:
        output.extend(
            render_interface_risk(
                neighborhood,
                cwd,
                changed_names,
                precise_callers,
            )
        )
    elif neighborhood.imported_by:
        names = ", ".join(display_path(path, cwd) for path in neighborhood.imported_by)
        output.append(f"    used by: {names}")
    return output


def compose_code_map(
    neighborhoods: list,
    cwd: Optional[str],
    tier: int,
    **render_state,
) -> str:
    """Compose the complete CodeMap reminder at one detail tier."""
    lines = [
        "# Code map",
        "Structure of files you're working with — what each defines and how "
        "they depend on each other (within this set). Use it to target "
        "Search/Read instead of re-scanning:",
        "",
    ]
    for neighborhood in neighborhoods:
        lines.extend(render_file(neighborhood, cwd, tier, **render_state))
    return "\n".join(lines).rstrip()


def render_code_map(
    neighborhoods: list,
    cwd: Optional[str],
    max_tokens: int,
    **render_state,
) -> str:
    """Render CodeMap rows, degrading details until the budget is met."""
    for tier in (0, 1):
        block = compose_code_map(neighborhoods, cwd, tier, **render_state)
        if count_tokens(block, tokenizer=DEFAULT_TEXT_TOKENIZER) <= max_tokens:
            return block
    return compose_code_map(neighborhoods, cwd, 2, **render_state)


__all__ = [
    "render_calls",
    "compose_code_map",
    "render_code_map",
    "render_defines",
    "render_interface_risk",
    "render_file",
    "render_surfaced_callers",
    "render_symbols",
    "render_unread_target",
    "render_unread_used_by",
]
