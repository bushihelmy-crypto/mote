"""Structural enrichment for Product CodeMap neighborhoods."""

from __future__ import annotations

from mote.contracts.ports.code_intelligence.code_map import CodeMapQueryPort


def is_public_interface(qualified_name: str) -> bool:
    """Return whether a symbol is a public, module-level interface."""
    return "." not in qualified_name and not qualified_name.startswith("_")


def diff_symbol_shapes(
    neighborhoods: list,
    shape_cache: dict[str, dict[str, str]],
) -> tuple[dict[str, list[str]], dict[str, list]]:
    """Diff fresh symbol shapes and update the caller-owned baseline cache."""
    changed_names: dict[str, list[str]] = {}
    changed_symbols: dict[str, list] = {}
    for neighborhood in neighborhoods:
        fresh = {symbol.qualified_name: symbol.signature for symbol in neighborhood.symbols}
        prior = shape_cache.get(neighborhood.path)
        shape_cache[neighborhood.path] = fresh
        if not prior:
            continue
        broken = [
            name for name in prior if is_public_interface(name) and (name not in fresh or fresh[name] != prior[name])
        ]
        if not broken:
            continue
        changed_names[neighborhood.path] = broken
        broken_set = set(broken)
        changed_symbols[neighborhood.path] = [
            symbol for symbol in neighborhood.symbols if symbol.qualified_name in broken_set
        ]
    return changed_names, changed_symbols


def fill_unread_from_index(
    neighborhoods: list,
    code_map,
    repo_index: CodeMapQueryPort,
) -> None:
    """Populate unread dependency metadata from the persistent repo index."""
    for neighborhood in neighborhoods:
        if not neighborhood.imports_unread:
            continue
        try:
            symbols, summaries, used_by = code_map.resolve_unread_from_index(
                neighborhood.imports_unread,
                symbols_of=repo_index.symbols_in,
                module_summary_of=repo_index.module_summary_of,
                importers_of=repo_index.importers,
                references_of=lambda path, symbol: [
                    (reference.path, reference.line) for reference in repo_index.references_to(path, symbol)
                ],
            )
            neighborhood.unread_symbols = symbols
            neighborhood.unread_module_summary = summaries
            neighborhood.unread_imported_by = used_by
        except Exception:  # noqa: BLE001 - advisory enrichment is best-effort
            pass


async def fill_unread_symbols(neighborhoods: list, code_map, lsp_query) -> None:
    """Overlay LSP-resolved symbols on the repo-index baseline."""
    for neighborhood in neighborhoods:
        if not neighborhood.imports_unread:
            continue
        try:
            lsp_symbols = await code_map.resolve_unread(
                neighborhood.path,
                neighborhood.imports_unread,
                lsp_query,
            )
        except Exception:  # noqa: BLE001 - advisory enrichment is best-effort
            lsp_symbols = {}
        neighborhood.unread_symbols = {
            **neighborhood.unread_symbols,
            **lsp_symbols,
        }


async def resolve_precise_callers(
    neighborhoods: list,
    changed_symbols: dict[str, list],
    code_map,
    lsp_query,
) -> dict[str, dict[str, list[str]]]:
    """Resolve callers for symbols whose public interface changed."""
    resolved: dict[str, dict[str, list[str]]] = {}
    by_path = {neighborhood.path: neighborhood for neighborhood in neighborhoods}
    for path, symbols in changed_symbols.items():
        if by_path.get(path) is None or not symbols:
            continue
        try:
            resolved[path] = await code_map.precise_callers(
                path,
                symbols,
                lsp_query,
            )
        except Exception:  # noqa: BLE001 - advisory enrichment is best-effort
            resolved[path] = {}
    return resolved


async def resolve_surfaced_callers(
    neighborhoods: list,
    changed_names: dict[str, list[str]],
    code_map,
    lsp_query,
) -> dict[str, dict[str, list[str]]]:
    """Resolve callers for calm public symbols surfaced in the working set."""
    resolved: dict[str, dict[str, list[str]]] = {}
    for neighborhood in neighborhoods:
        if neighborhood.path in changed_names:
            continue
        targets = [symbol for symbol in neighborhood.symbols if is_public_interface(symbol.qualified_name)]
        if not targets:
            continue
        try:
            resolved[neighborhood.path] = await code_map.precise_callers(
                neighborhood.path,
                targets,
                lsp_query,
            )
        except Exception:  # noqa: BLE001 - advisory enrichment is best-effort
            resolved[neighborhood.path] = {}
    return resolved


def resolve_in_context(
    neighborhoods: list,
    get_read_state,
    *,
    post_compact: bool,
) -> tuple[set[str], bool]:
    """Resolve which observed file bodies remain live in model context."""
    if get_read_state is None:
        return set(), post_compact
    if post_compact:
        return set(), False
    try:
        read_state = get_read_state() or {}
    except Exception:  # noqa: BLE001 - advisory enrichment is best-effort
        read_state = {}
    return {neighborhood.path for neighborhood in neighborhoods if read_state.get(neighborhood.path) is not None}, False


def neighborhood_has_content(
    neighborhood,
    changed_names: dict[str, list[str]],
    in_context: set[str],
) -> bool:
    """Return whether a neighborhood would contribute a visible row."""
    if neighborhood.path in changed_names:
        return True
    if neighborhood.imports or neighborhood.imported_by or neighborhood.imports_unread:
        return True
    if neighborhood.path in in_context:
        return False
    return bool(neighborhood.symbols or neighborhood.calls)


def neighborhood_signature(
    neighborhood,
    *,
    in_context: set[str],
    changed_names: dict[str, list[str]],
    precise_callers: dict[str, dict[str, list[str]]],
    surfaced_callers: dict[str, dict[str, list[str]]],
) -> str:
    """Build the stable incremental signature for one enriched neighborhood."""
    symbols = ",".join(f"{symbol.qualified_name}:{symbol.summary}" for symbol in neighborhood.symbols)
    imports = ",".join(neighborhood.imports)
    unread = ",".join(neighborhood.imports_unread)
    used_by = ",".join(neighborhood.imported_by)
    calls = ",".join(sorted({f"{call.caller}>{call.callee}" for call in neighborhood.calls}))
    resolved = ";".join(
        f"{path}:{','.join(neighborhood.unread_symbols[path])}" for path in sorted(neighborhood.unread_symbols or {})
    )
    unread_purpose = ";".join(
        f"{path}:{neighborhood.unread_module_summary[path]}"
        for path in sorted(neighborhood.unread_module_summary or {})
    )
    unread_used = ";".join(
        f"{path}:{','.join(neighborhood.unread_imported_by[path])}"
        for path in sorted(neighborhood.unread_imported_by or {})
    )
    context_bit = "1" if neighborhood.path in in_context else "0"
    risk = ",".join(changed_names.get(neighborhood.path, []))
    precise = ";".join(
        f"{symbol}:{','.join(callers)}"
        for symbol, callers in sorted((precise_callers.get(neighborhood.path) or {}).items())
    )
    surfaced = ";".join(
        f"{symbol}:{','.join(callers)}"
        for symbol, callers in sorted((surfaced_callers.get(neighborhood.path) or {}).items())
    )
    return (
        f"{neighborhood.module_summary}|{symbols}|{imports}|{unread}|{used_by}|"
        f"{calls}|{resolved}|{unread_purpose}|{unread_used}|{context_bit}|"
        f"{risk}|{precise}|{surfaced}"
    )


__all__ = [
    "diff_symbol_shapes",
    "fill_unread_from_index",
    "fill_unread_symbols",
    "is_public_interface",
    "neighborhood_has_content",
    "neighborhood_signature",
    "resolve_precise_callers",
    "resolve_in_context",
    "resolve_surfaced_callers",
]
