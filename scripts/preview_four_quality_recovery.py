"""Read-only reviewer preview that overlays valid focused four-app recoveries.

It never writes SQLite, trace history, or report output.  Focused trace values
replace an earlier field only when the new value is present and the field is
eligible for the reviewer contract.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline.reviewer_view import _evidenced_auth_methods, _is_reviewer_ready_description, reviewer_row
from pipeline.schema import AppFinding, Evidence

APPS = ("salesforce", "hubspot", "slack", "twilio")
TRACE_ROOT = ROOT / "data" / "logs" / "proof_traces"


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _base_run() -> Path:
    complete = [p for p in TRACE_ROOT.iterdir() if p.is_dir() and p.name.startswith("run_") and all((p / f"{app}.json").is_file() for app in APPS)]
    if not complete:
        raise SystemExit("No complete four-app pilot trace found.")
    return max(complete, key=lambda p: p.stat().st_mtime)


def _eligible_evidence(recovered: AppFinding, fields: set[str]) -> list[Evidence]:
    return [e for e in recovered.evidence if e.field in fields]


def _overlay(base: AppFinding, slug: str) -> AppFinding:
    """Merge only usable focused recovery fields, oldest to newest."""
    result = base
    for directory in sorted(p for p in TRACE_ROOT.iterdir() if p.is_dir() and p.name.startswith("focused_")):
        path = directory / f"{slug}.json"
        if not path.exists():
            continue
        payload = _read(path)
        recovered_data = payload.get("recovered")
        if not recovered_data:
            continue
        recovered = AppFinding.model_validate(recovered_data)
        changes: dict[str, object] = {}
        accepted_fields: set[str] = set()
        if _is_reviewer_ready_description(recovered.one_liner):
            changes["one_liner"] = recovered.one_liner
            accepted_fields.add("one_liner")
        if recovered.auth_methods:
            supported = _evidenced_auth_methods(recovered)
            if supported:
                changes["auth_methods"] = list(dict.fromkeys([*result.auth_methods, *supported]))
                accepted_fields.add("auth_methods")
        if recovered.api_surface_type != "unknown":
            changes["api_surface_type"] = recovered.api_surface_type
            accepted_fields.add("api_surface_type")
        if recovered.api_breadth_notes:
            changes["api_breadth_notes"] = recovered.api_breadth_notes
            changes["api_surface_summary"] = recovered.api_surface_summary or recovered.api_breadth_notes
            accepted_fields.add("api_breadth_notes")
        if not changes:
            continue
        # Evidence is append-only in the view as well.  The projection helper
        # selects the latest item per field, preserving field provenance.
        changes["evidence"] = [*result.evidence, *_eligible_evidence(recovered, accepted_fields)]
        result = result.model_copy(update=changes)
    return result


def main() -> None:
    base_dir = _base_run()
    print(f"Read-only overlay: {base_dir.name} + focused field recoveries")
    print("| App | Category | What it does | Auth | Access | API Surface | MCP | Buildability | Main Blocker / Caveat | Evidence / Sources |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    rows = []
    for slug in APPS:
        trace = _read(base_dir / f"{slug}.json")
        finding = _overlay(AppFinding.model_validate(trace["validation"]["finding"]), slug)
        row = reviewer_row(trace["app"], finding)
        rows.append(row)
        links = " · ".join(f"[{item['label']}]({item['url']})" for item in row.evidence)
        values = [row.app, row.category, row.what_it_does, row.auth, row.access, row.api_surface.replace("\n", "<br>"), row.mcp, row.buildability, row.main_blocker_or_caveat, links]
        print("| " + " | ".join(value.replace("|", "\\|") for value in values) + " |")
    print()
    for row in rows:
        print(f"- **{row.app}** — Grounded fields: {', '.join(row.grounded_fields)}. Unresolved fields: {', '.join(row.unresolved_fields) or 'None'}. Research status: {row.research_status}. Evidence coverage: {row.evidence_coverage}.")


if __name__ == "__main__":
    main()
