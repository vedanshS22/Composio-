"""Build a static reviewer report from one append-only quality-trace run.

This is deliberately read-only with respect to research history: it projects
the trace payloads that already exist and represents records without a valid
finding as field-level unresolved.  It does not invoke the quality gate.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline.analysis import patterns
from pipeline.reviewer_view import reviewer_row
from pipeline.schema import AppFinding, AppSeed
from pipeline.storage import list_seeds


def _slug(name: str) -> str:
    return "".join(char.lower() if char.isalnum() else "_" for char in name).strip("_")


def _unresolved(seed: AppSeed, record: dict) -> AppFinding:
    """Represent an absent extraction without inventing a product conclusion."""
    failure_sections = ("run_failure", "llm_or_validation", "quality_recovery")
    blockers = [section for section in failure_sections if isinstance(record.get(section), dict) and record[section].get("error")]
    return AppFinding(
        app_id=seed.id,
        category=seed.category,
        confidence=0,
        needs_human_review=True,
        model_used="unavailable",
        research_status="unresolved",
        research_blockers=blockers or ["no_validated_finding"],
    )


def findings_from_trace(trace_dir: Path | list[Path], seeds: list[AppSeed]) -> list[AppFinding]:
    """Project one or more append-only trace overlays, last directory wins."""
    trace_dirs = trace_dir if isinstance(trace_dir, list) else [trace_dir]
    findings: list[AppFinding] = []
    for seed in seeds:
        record: dict = {}
        for directory in trace_dirs:
            path = directory / f"{_slug(seed.name)}.json"
            if path.is_file():
                record = json.loads(path.read_text(encoding="utf-8"))
        validation = record.get("validation") if isinstance(record, dict) else None
        payload = validation.get("finding") if isinstance(validation, dict) else None
        findings.append(AppFinding.model_validate(payload) if isinstance(payload, dict) else _unresolved(seed, record))
    return findings


def build_trace_report(trace_dir: Path | list[Path], output: Path) -> None:
    """Project the traces present so far into a standalone static report."""
    all_seeds = list_seeds(ROOT / "data" / "research.db")
    if len(all_seeds) != 100:
        raise ValueError(f"Expected 100 fixed seed apps; found {len(all_seeds)}")
    findings = findings_from_trace(trace_dir, all_seeds)
    names = {seed.id: seed.name for seed in all_seeds}
    app_rows = [{**finding.model_dump(mode="json"), "name": names[finding.app_id]} for finding in findings]
    reviewer_rows = [asdict(reviewer_row(names[finding.app_id], finding)) for finding in findings]
    verification = {"fields_checked": 0, "matched": 0, "accuracy": None, "pass1_accuracy": None, "pass2_accuracy": None, "mismatches": []}
    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_trace_directory": [str(item) for item in trace_dir] if isinstance(trace_dir, list) else str(trace_dir),
        "apps": app_rows,
        "reviewer_apps": reviewer_rows,
        "patterns": patterns(findings),
        "verification": verification,
        "report_scope": len(all_seeds),
    }
    env = Environment(loader=FileSystemLoader(ROOT / "report" / "templates"), autoescape=select_autoescape(["html"]))
    rendered = env.get_template("index.html.j2").render(
        data=data, apps=reviewer_rows, patterns=data["patterns"], verification=verification,
        data_json=json.dumps(data).replace("</", "<\\/"), report_scope=len(all_seeds),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a static 100-app report from append-only trace files.")
    parser.add_argument("--trace-dir", type=Path, action="append", required=True, help="Append-only trace directory; later entries overlay earlier entries.")
    parser.add_argument("--output", type=Path, default=ROOT / "report" / "dist" / "index_100_raw.html")
    parser.add_argument(
        "--apps",
        help="Optional comma-separated canonical seed names. Omitting this projects all fixed seed apps.",
    )
    args = parser.parse_args()
    trace_dirs = [item.resolve() for item in args.trace_dir]
    missing = [str(item) for item in trace_dirs if not item.is_dir()]
    if missing:
        raise SystemExit(f"Trace directory does not exist: {', '.join(missing)}")

    all_seeds = list_seeds(ROOT / "data" / "research.db")
    if len(all_seeds) != 100:
        raise SystemExit(f"Expected 100 fixed seed apps; found {len(all_seeds)}")
    seeds = all_seeds
    if args.apps:
        requested_names = [name.strip() for name in args.apps.split(",") if name.strip()]
        canonical = {seed.name.casefold(): seed for seed in all_seeds}
        unknown = [name for name in requested_names if name.casefold() not in canonical]
        if unknown:
            raise SystemExit(f"Unknown seed app(s): {', '.join(unknown)}")
        seeds = [canonical[name.casefold()] for name in requested_names]
    if len(seeds) != len(all_seeds):
        # A scoped report intentionally contains only its requested trace set.
        # Keep the CLI behavior for a three-app artifact without affecting the
        # runner's 100-app live projection.
        findings = findings_from_trace(trace_dirs, seeds)
        names = {seed.id: seed.name for seed in seeds}
        app_rows = [{**finding.model_dump(mode="json"), "name": names[finding.app_id]} for finding in findings]
        reviewer_rows = [asdict(reviewer_row(names[finding.app_id], finding)) for finding in findings]
        verification = {"fields_checked": 0, "matched": 0, "accuracy": None, "pass1_accuracy": None, "pass2_accuracy": None, "mismatches": []}
        data = {"generated_at": datetime.now(timezone.utc).isoformat(), "source_trace_directory": [str(item) for item in trace_dirs], "apps": app_rows, "reviewer_apps": reviewer_rows, "patterns": patterns(findings), "verification": verification, "report_scope": len(seeds)}
        env = Environment(loader=FileSystemLoader(ROOT / "report" / "templates"), autoescape=select_autoescape(["html"]))
        rendered = env.get_template("index.html.j2").render(data=data, apps=reviewer_rows, patterns=data["patterns"], verification=verification, data_json=json.dumps(data).replace("</", "<\\/"), report_scope=len(seeds))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        build_trace_report(trace_dirs, args.output)
    print(f"Built {args.output}")


if __name__ == "__main__":
    main()
