"""Calculate an evidence-first quality gate from one append-only trace run."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline.schema import AppFinding


def _has_api(finding: AppFinding) -> bool:
    return bool(finding.api_surface_types) or finding.api_surface_type != "unknown"


def _field_coverage(finding: AppFinding) -> dict[str, bool]:
    evidence_fields = {item.field for item in finding.evidence}
    return {
        "what_it_does": bool(finding.one_liner) and "one_liner" in evidence_fields,
        "auth": bool(finding.auth_methods) and "auth_methods" in evidence_fields,
        "access": finding.self_serve_status != "unknown" and "self_serve_status" in evidence_fields,
        "api_surface": _has_api(finding) and bool({"api_surface_types", "api_surface_type", "api_breadth_notes", "api_surface_summary"} & evidence_fields),
        "mcp": finding.mcp_status != "unknown" and bool({"mcp_status", "mcp_exists"} & evidence_fields),
        "buildability": finding.buildability_verdict is not None and "buildability_verdict" in evidence_fields,
        "evidence": bool(finding.evidence),
    }


def evaluate(trace_dir: Path) -> dict:
    rows = []
    failures = []
    for path in sorted(trace_dir.glob("*.json")):
        if path.name in {"summary.json", "quality_gate.json"}:
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        finding_payload = payload.get("validation", {}).get("finding")
        if not isinstance(finding_payload, dict):
            failures.append({"app": payload.get("app", path.stem), "failure": payload.get("run_failure") or payload.get("llm_or_validation") or "no validated finding"})
            continue
        finding = AppFinding.model_validate(finding_payload)
        rows.append((payload.get("app", path.stem), finding, payload))
    statuses = Counter(finding.research_status for _, finding, _ in rows)
    coverage = Counter()
    for _, finding, _ in rows:
        coverage.update(key for key, present in _field_coverage(finding).items() if present)
    unknown_fields = sum(sum(not present for present in _field_coverage(finding).values()) for _, finding, _ in rows)
    blockers = sum(bool(finding.research_blockers) for _, finding, _ in rows)
    recoveries = sum(bool(payload.get("quality_recovery", {}).get("targets")) for _, _, payload in rows)
    transient = sum(len(payload.get("transient_retries", [])) for _, _, payload in rows)
    total = len(rows) + len(failures)
    systemic = []
    for field in ("auth", "access", "api_surface", "mcp", "evidence"):
        if total and coverage[field] / total < 0.2:
            systemic.append(f"low {field} coverage: {coverage[field]}/{total}")
    if failures:
        systemic.append(f"infrastructure failures: {len(failures)}/{total}")
    return {
        "total_apps": total, "grounded": statuses["grounded"] + statuses["complete"],
        "partial": statuses["partial"], "unresolved": statuses["unresolved"] + len(failures),
        "field_coverage": dict(coverage), "genuine_unresolved_fields": unknown_fields,
        "apps_with_research_blockers": blockers, "focused_recoveries": recoveries,
        "transient_failures_retried": transient, "failures": failures, "systemic_findings": systemic,
        "quality_gate_pass": total == 100 and not systemic,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace-dir", required=True, type=Path)
    args = parser.parse_args()
    result = evaluate(args.trace_dir)
    (args.trace_dir / "quality_gate.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
