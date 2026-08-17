from __future__ import annotations
import csv, json, sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pipeline.analysis import patterns, verification_summary
from pipeline.reviewer_view import reviewer_row
from pipeline.storage import latest_findings, list_seeds, verification_rows
def main() -> None:
    db = ROOT / "data/research.db"
    findings = [finding for _, finding in latest_findings(db)]
    if not findings: raise SystemExit("No findings to report. Run research first.")
    names = {seed.id: seed.name for seed in list_seeds(db)}
    app_rows = [{**f.model_dump(mode="json"), "name": names[f.app_id]} for f in findings]
    reviewer_rows = [asdict(reviewer_row(names[f.app_id], f)) for f in findings]
    data = {"generated_at": datetime.now(timezone.utc).isoformat(), "apps": app_rows, "reviewer_apps": reviewer_rows, "patterns": patterns(findings), "verification": verification_summary(verification_rows(db))}
    env = Environment(loader=FileSystemLoader(ROOT / "report/templates"), autoescape=select_autoescape(["html"]))
    rendered = env.get_template("index.html.j2").render(data=data, apps=data["reviewer_apps"], patterns=data["patterns"], verification=data["verification"], data_json=json.dumps(data).replace("</", "<\\/"))
    destination = ROOT / "report/dist/index.html"; destination.parent.mkdir(parents=True, exist_ok=True); destination.write_text(rendered, encoding="utf-8")
    export = ROOT / "data/exports/results.json"; export.write_text(json.dumps(data, indent=2), encoding="utf-8")
    with (ROOT / "data/exports/results.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["app_id", "category", "one_liner", "auth_methods", "self_serve_status", "api_surface_type", "buildability_verdict", "blocker", "evidence"]); writer.writeheader()
        for app in data["apps"]:
            writer.writerow({field: ({"auth_methods": "; ".join(app["auth_methods"]), "evidence": "; ".join(e["url"] for e in app["evidence"])}.get(field, app.get(field))) for field in writer.fieldnames})
    print(f"Built {destination}")
if __name__ == "__main__": main()
