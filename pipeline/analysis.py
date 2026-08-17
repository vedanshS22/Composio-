from __future__ import annotations

from collections import Counter, defaultdict
from pipeline.schema import AppFinding


def patterns(findings: list[AppFinding]) -> dict:
    auth = Counter(method for finding in findings for method in finding.auth_methods)
    self_serve = Counter(f.self_serve_status for f in findings)
    verdicts = Counter(f.buildability_verdict for f in findings)
    blockers = Counter(f.blocker for f in findings if f.blocker)
    by_category: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "self_serve": 0, "gated": 0})
    for f in findings:
        row = by_category[f.category]; row["total"] += 1
        if f.self_serve_status in ("self_serve_free", "self_serve_trial"): row["self_serve"] += 1
        if f.self_serve_status in ("paid_plan_required", "admin_approval_required", "partner_gated"): row["gated"] += 1
    return {"total_apps": len(findings), "auth_distribution": dict(auth), "self_serve_split": dict(self_serve), "buildability_split": dict(verdicts), "top_blockers": [{"blocker": key, "count": value} for key, value in blockers.most_common(5)], "category_gating_skew": {category: {**counts, "self_serve_pct": round(100 * counts["self_serve"] / counts["total"], 1) if counts["total"] else 0} for category, counts in by_category.items()}}


def verification_summary(rows: list[dict]) -> dict:
    # Retries are preserved in SQLite, but an accuracy denominator must count
    # one latest independent comparison per app/field/source, not retry rows twice.
    latest: dict[tuple[str, str, str], dict] = {}
    for row in rows:
        key = (row["app"], row["field_name"], row["verifier_source"])
        if key not in latest or row["id"] > latest[key]["id"]:
            latest[key] = row
    rows = list(latest.values())
    def metric(source: str) -> dict:
        subset = [r for r in rows if r["verifier_source"] == source]
        matched = sum(bool(r["match"]) for r in subset)
        return {"fields_checked": len(subset), "matched": matched, "accuracy": round(100 * matched / len(subset), 1) if subset else None}
    pass1 = metric("independent_docs_agent")
    pass2 = metric("independent_docs_agent_pass2")
    return {"fields_checked": pass1["fields_checked"] + pass2["fields_checked"], "matched": pass1["matched"] + pass2["matched"], "accuracy": pass2["accuracy"] if pass2["accuracy"] is not None else pass1["accuracy"], "pass1_accuracy": pass1["accuracy"], "pass2_accuracy": pass2["accuracy"], "pass1_fields_checked": pass1["fields_checked"], "pass2_fields_checked": pass2["fields_checked"], "mismatches": [{"app": r["app"], "field": r["field_name"], "pipeline_value": r["pipeline_value"], "verified_value": r["verified_value"], "notes": r["notes"]} for r in rows if r["verifier_source"] == "independent_docs_agent" and not r["match"]]}
