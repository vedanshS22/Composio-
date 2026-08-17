"""Render the latest completed four-app proof trace as the reviewer table."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pipeline.reviewer_view import reviewer_row
from pipeline.schema import AppFinding

APPS = ("salesforce", "hubspot", "slack", "twilio")


def latest_completed_run() -> Path:
    base = ROOT / "data/logs/proof_traces"
    candidates = [path for path in base.iterdir() if path.is_dir() and all((path / f"{app}.json").is_file() for app in APPS)]
    if not candidates:
        raise SystemExit("No completed four-app proof trace directory found.")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def main() -> None:
    run = latest_completed_run()
    rows = []
    for slug in APPS:
        trace = json.loads((run / f"{slug}.json").read_text(encoding="utf-8"))
        payload = trace["validation"]["finding"]
        rows.append(reviewer_row(trace["app"], AppFinding.model_validate(payload)))
    print(f"Trace: {run}")
    print("| App | Category | What it does | Auth | Access | API Surface | MCP | Buildability | Main Blocker / Caveat | Evidence |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for row in rows:
        evidence = " · ".join(f"[{item['label']}]({item['url']})" for item in row.evidence)
        fields = [row.app, row.category, row.what_it_does, row.auth, row.access, row.api_surface.replace("\n", "<br>"), row.mcp, row.buildability, row.main_blocker_or_caveat, evidence]
        print("| " + " | ".join(value.replace("|", "\\|") for value in fields) + " |")
    print()
    for row in rows:
        unresolved = ", ".join(row.unresolved_fields) or "None"
        print(f"- **{row.app}** — grounded: {', '.join(row.grounded_fields)}; unresolved: {unresolved}; evidence coverage: {row.evidence_coverage}; research status: {row.research_status}.")


if __name__ == "__main__":
    main()
