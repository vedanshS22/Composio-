"""Read append-only Scout100 traces and report evidence-quality coverage.

This command is deliberately read-only: it never updates SQLite, historical
traces, or the HTML report.  Later traces may be supplied as overlays for the
same canonical seed app.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


VISIBLE_FIELDS = (
    "one_liner", "auth_methods", "self_serve_status", "api_surface_types",
    "api_breadth_notes", "mcp_status", "buildability_verdict",
)


def _finding(record: dict) -> dict | None:
    validation = record.get("validation")
    value = validation.get("finding") if isinstance(validation, dict) else None
    return value if isinstance(value, dict) else None


def _records(directory: Path) -> dict[str, dict]:
    records: dict[str, dict] = {}
    for path in directory.glob("*.json"):
        if path.name == "summary.json":
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        app = record.get("app")
        finding = _finding(record)
        if isinstance(app, str) and finding:
            records[app.casefold()] = {"app": app, "finding": finding, "record": record}
    return records


def _grounded(field: str, finding: dict) -> bool:
    value = finding.get(field)
    return value not in (None, "unknown", [], "")


def summarize(directories: list[Path]) -> dict:
    merged: dict[str, dict] = {}
    for directory in directories:
        merged.update(_records(directory))
    findings = [item["finding"] for item in merged.values()]
    coverage = {
        "Category (fixed seed)": len(findings),
        "What it does": sum(_grounded("one_liner", item) for item in findings),
        "Auth": sum(_grounded("auth_methods", item) for item in findings),
        "Access": sum(_grounded("self_serve_status", item) for item in findings),
        "API Surface": sum(_grounded("api_surface_types", item) for item in findings),
        "API Breadth": sum(_grounded("api_breadth_notes", item) for item in findings),
        "MCP": sum(_grounded("mcp_status", item) for item in findings),
        "Buildability": sum(_grounded("buildability_verdict", item) for item in findings),
        "Evidence": sum(bool(item.get("evidence")) for item in findings),
    }
    statuses = Counter(item.get("research_status", "unresolved") for item in findings)
    fully_unknown = sum(not any(_grounded(field, item) for field in VISIBLE_FIELDS) for item in findings)
    blockers = [blocker for item in findings for blocker in item.get("research_blockers", [])]
    research_failures = sum(blocker.startswith("research_execution_failure:") for blocker in blockers)
    infrastructure_failed = sum(
        any(marker in blocker.lower() for marker in ("research_execution_failure", "fetch failed", "source discovery failed"))
        for blocker in blockers
    )
    genuine_unknown = sum(
        not _grounded(field, item)
        and not any("research_execution_failure" in blocker for blocker in item.get("research_blockers", []))
        for item in findings for field in VISIBLE_FIELDS
    )
    return {
        "total_apps": len(findings),
        "grounded": statuses["grounded"] + statuses["complete"],
        "partial": statuses["partial"],
        "unresolved": statuses["unresolved"],
        "research_failure_blockers": research_failures,
        "field_coverage": coverage,
        "access_distribution": dict(sorted(Counter(item.get("self_serve_status", "unknown") for item in findings).items())),
        "fully_unknown_rows": fully_unknown,
        "genuine_unknown_fields": genuine_unknown,
        "infrastructure_failure_blockers": infrastructure_failed,
        "multi_auth_findings": sum(len(item.get("auth_methods", [])) > 1 for item in findings),
        "multi_api_findings": sum(len(item.get("api_surface_types", [])) > 1 for item in findings),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only summary of append-only quality traces.")
    parser.add_argument("trace_dirs", nargs="+", type=Path, help="Trace/recovery directories in increasing precedence order.")
    args = parser.parse_args()
    missing = [str(path) for path in args.trace_dirs if not path.is_dir()]
    if missing:
        raise SystemExit("Missing trace directory: " + ", ".join(missing))
    print(json.dumps(summarize(args.trace_dirs), indent=2))


if __name__ == "__main__":
    main()
