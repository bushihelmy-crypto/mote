"""Static validator for the core architecture debt implementation ledger.

The implementation document is the sole status authority.  This module only
parses that document; importing it cannot construct Product composition or
discover checkout extensions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LEDGER_PATH = ROOT / "zdocs/core-architecture-debt-closure-implementation.md"

WORK_PACKAGE = re.compile(r"R\d+\.\d+")
WORK_PACKAGE_HEADING = re.compile(r"^### (R\d+\.\d+)\b", re.MULTILINE)
ADR = re.compile(r"ADR-D\d+")
PRODUCTION_PATH = re.compile(r"`((?:contracts|kernel|runtime|orchestration|product)/[^`\n]+\.py)(?::\d+(?:-\d+)?)?`")
# Historical debt documents intentionally retain references to retired owners;
# those paths are evidence of the debt, not current production dependencies.
RETIRED_PRODUCTION_PATHS = frozenset(
    {
        "product/interfaces/inference_admin_api/application.py",
        "runtime/agent/runtime_maintenance.py",
        "runtime/durable/backend.py",
        "runtime/ledger/run_journal.py",
        "runtime/models/auth/oauth/storage/fallback_store.py",
        "runtime/session/workspace/cleanup.py",
        "runtime/session/workspace/cleanup_gate.py",
    }
)
ALLOWED_STATUSES = frozenset(
    {
        "TODO",
        "CONFIRMED",
        "DECISION_REQUIRED",
        "NEEDS_EVIDENCE",
        "IN_PROGRESS",
        "BLOCKED",
        "DONE",
        "REJECTED",
    }
)
ACTIVE_STATUSES = frozenset({"IN_PROGRESS", "DONE"})


@dataclass(frozen=True)
class LedgerEntry:
    milestone: str
    work_package: str
    status: str
    owner: str
    blockers: str
    evidence: str


@dataclass(frozen=True)
class LedgerValidation:
    work_package_count: int
    dependency_count: int
    production_path_count: int


def _section(document: str, start: str, end: str) -> str:
    try:
        return document.split(start, 1)[1].split(end, 1)[0]
    except IndexError as error:
        raise AssertionError(f"missing ledger section boundary: {start!r} / {end!r}") from error


def _table_rows(section: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells and cells[0] not in {"里程碑", "---", "ADR"}:
            rows.append(cells)
    return rows


def _entries(document: str) -> dict[str, LedgerEntry]:
    section = _section(document, "### 10.5 工作包执行台账", "### 10.6 每个里程碑的签收步骤")
    entries: dict[str, LedgerEntry] = {}
    for cells in _table_rows(section):
        if len(cells) != 6:
            raise AssertionError(f"work-package ledger row must have six cells: {cells!r}")
        entry = LedgerEntry(*cells)
        if entry.work_package in entries:
            raise AssertionError(f"duplicate ledger work package: {entry.work_package}")
        entries[entry.work_package] = entry
    return entries


def _milestones(entries: dict[str, LedgerEntry]) -> dict[str, frozenset[str]]:
    result: dict[str, set[str]] = {}
    for entry in entries.values():
        result.setdefault(entry.milestone, set()).add(entry.work_package)
    return {name: frozenset(packages) for name, packages in result.items()}


def _dependencies(entries: dict[str, LedgerEntry]) -> dict[str, frozenset[str]]:
    milestones = _milestones(entries)
    dependencies: dict[str, frozenset[str]] = {}
    for package, entry in entries.items():
        direct = set(WORK_PACKAGE.findall(entry.blockers))
        for milestone in re.findall(r"\bM\d+\b", entry.blockers):
            if milestone not in milestones:
                raise AssertionError(f"{package} references unknown milestone {milestone}")
            direct.update(milestones[milestone])
        direct.discard(package)
        dependencies[package] = frozenset(direct)
    return dependencies


def _assert_acyclic(dependencies: dict[str, frozenset[str]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(package: str, trail: tuple[str, ...]) -> None:
        if package in visiting:
            cycle = " -> ".join((*trail, package))
            raise AssertionError(f"work-package dependency cycle: {cycle}")
        if package in visited:
            return
        visiting.add(package)
        for dependency in sorted(dependencies[package]):
            visit(dependency, (*trail, package))
        visiting.remove(package)
        visited.add(package)

    for package in sorted(dependencies):
        visit(package, ())


def validate_ledger(document: str, *, root: Path = ROOT) -> LedgerValidation:
    body = _section(document, "## 4. P0", "## 8. 合理保留项")
    headings = WORK_PACKAGE_HEADING.findall(body)
    if len(headings) != len(set(headings)):
        raise AssertionError("work-package headings must be unique")

    entries = _entries(document)
    heading_set = set(headings)
    entry_set = set(entries)
    if heading_set != entry_set:
        missing = sorted(heading_set - entry_set)
        unknown = sorted(entry_set - heading_set)
        raise AssertionError(f"ledger/title mismatch; missing={missing}, unknown={unknown}")

    dependencies = _dependencies(entries)
    for package, direct in dependencies.items():
        unknown = direct - entry_set
        if unknown:
            raise AssertionError(f"{package} references unknown work packages: {sorted(unknown)}")
    _assert_acyclic(dependencies)

    adr_section = _section(document, "### 10.3 产品决策登记", "### 10.4 里程碑总览")
    declared_adrs = set(ADR.findall(adr_section))
    for entry in entries.values():
        if entry.status not in ALLOWED_STATUSES:
            raise AssertionError(f"{entry.work_package} has invalid status {entry.status!r}")
        if entry.status in ACTIVE_STATUSES:
            if entry.owner in {"", "—"}:
                raise AssertionError(f"{entry.work_package} requires a unique Owner")
            if entry.status == "DONE":
                incomplete = [
                    dependency
                    for dependency in dependencies[entry.work_package]
                    if entries[dependency].status not in {"DONE", "REJECTED"}
                ]
                if incomplete:
                    raise AssertionError(f"{entry.work_package} has incomplete prerequisites: {sorted(incomplete)}")
        if entry.status == "DECISION_REQUIRED":
            linked = set(ADR.findall(entry.blockers + " " + entry.evidence))
            if not linked or not linked <= declared_adrs:
                raise AssertionError(f"{entry.work_package} requires a declared ADR link")
        if entry.status == "DONE":
            required_evidence = ("实现:", "测试:", "归零:")
            missing = [marker for marker in required_evidence if marker not in entry.evidence]
            if missing or not re.search(r"测试:[^|]*\b\d+\b", entry.evidence):
                raise AssertionError(f"{entry.work_package} DONE evidence is incomplete; missing={missing}")

    paths = set(PRODUCTION_PATH.findall(document))
    missing_paths = sorted(
        path for path in paths if path not in RETIRED_PRODUCTION_PATHS and not (root / path).is_file()
    )
    if missing_paths:
        raise AssertionError(f"document references missing production paths: {missing_paths}")

    return LedgerValidation(
        work_package_count=len(entries),
        dependency_count=sum(len(items) for items in dependencies.values()),
        production_path_count=len(paths),
    )


def validate_committed_ledger() -> LedgerValidation:
    return validate_ledger(LEDGER_PATH.read_text(encoding="utf-8"))
