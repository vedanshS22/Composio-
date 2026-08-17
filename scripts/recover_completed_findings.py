"""Append-only generic recovery for completed Scout100 trace records.

This command never rewrites the source trace directory.  It takes every
completed finding from that directory, preserves its grounded values, and runs
at most one compact focused recovery for the fields selected by the common
quality contract.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.composio_mcp import ComposioResearchMCP
from pipeline.env_loader import load_dotenv
from pipeline.quality_orchestration import run_isolated_batch
from pipeline.quality_recovery import recover_quality, targets_for
from pipeline.schema import AppFinding, AppSeed
from pipeline.storage import list_seeds
from scripts.run_quality_research import _sanitize_error, _slug


def completed_records(trace_dir: Path, seeds: list[AppSeed]) -> list[tuple[AppSeed, dict]]:
    """Read only valid, already-persisted trace findings by canonical seed name."""
    by_name = {seed.name.casefold(): seed for seed in seeds}
    records: list[tuple[AppSeed, dict]] = []
    for path in sorted(trace_dir.glob("*.json")):
        if path.name == "summary.json":
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        app = record.get("app")
        validation = record.get("validation")
        if not isinstance(app, str) or not isinstance(validation, dict) or not isinstance(validation.get("finding"), dict):
            continue
        seed = by_name.get(app.casefold())
        if seed:
            records.append((seed, record))
    return records


def validation_output(finding: AppFinding) -> dict:
    return {
        "research_status": finding.research_status,
        "evidence_fields": [item.field for item in finding.evidence],
        "research_blockers": finding.research_blockers,
        "buildability_verdict": finding.buildability_verdict,
        "finding": finding.model_dump(mode="json"),
    }


async def recover_one(
    seed: AppSeed, record: dict, requested_fields: tuple[str, ...] | None = None,
    allow_llm: bool = True,
) -> dict:
    existing = AppFinding.model_validate(record["validation"]["finding"])
    automatic_targets = targets_for(existing)
    fields = tuple(field for field in (requested_fields or automatic_targets) if field in automatic_targets)
    result = {
        "app": seed.name,
        "source_trace": record.get("app"),
        "source_status": existing.research_status,
        "targets": list(fields),
    }
    if not fields:
        result["recovery"] = {"status": "not_needed"}
        result["validation"] = validation_output(existing)
        return result
    try:
        client = await asyncio.to_thread(ComposioResearchMCP)
        recovery = await recover_quality(seed, existing, client, fields=fields, allow_llm=allow_llm)
        final_payload = recovery.get("finding")
        final = AppFinding.model_validate(final_payload) if isinstance(final_payload, dict) else existing
        result["recovery"] = recovery
        result["validation"] = validation_output(final)
    except Exception as exc:
        # A recovery failure is audit metadata, never a product-field value.
        result["recovery"] = {"status": "failure", "error": _sanitize_error(exc)}
        result["validation"] = validation_output(existing)
    return result


async def main_async(args: argparse.Namespace) -> Path:
    load_dotenv()
    source_dir = args.trace_dir.resolve()
    if not source_dir.is_dir():
        raise ValueError(f"Trace directory does not exist: {source_dir}")
    seeds = list_seeds(ROOT / "data" / "research.db")
    records = completed_records(source_dir, seeds)
    if args.resume_dir:
        output_dir = args.resume_dir.resolve()
        if not output_dir.is_dir():
            raise ValueError(f"resume directory does not exist: {output_dir}")
    else:
        output_dir = ROOT / "data" / "logs" / "completed_recovery" / datetime.now(UTC).strftime("recovery_%Y%m%dT%H%M%SZ")
        output_dir.mkdir(parents=True, exist_ok=False)

    requested_fields = tuple(dict.fromkeys(args.fields)) if args.fields else None

    completed_slugs = {path.stem for path in output_dir.glob("*.json") if path.name != "summary.json"}
    pending_records = [(seed, record) for seed, record in records if _slug(seed) not in completed_slugs]

    async def worker(seed: AppSeed, record: dict) -> dict:
        return await recover_one(seed, record, requested_fields, allow_llm=not args.source_only)

    async def persist(seed: AppSeed, result: dict) -> None:
        (output_dir / f"{_slug(seed)}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")

    async def wrapped(seed: AppSeed) -> dict:
        record = next(record for candidate, record in pending_records if candidate.id == seed.id)
        return await worker(seed, record)

    results = await run_isolated_batch([seed for seed, _ in pending_records], wrapped, concurrency=args.concurrency, on_complete=persist)
    summary = {
        "source_trace_directory": str(source_dir),
        "completed_apps": len(completed_slugs) + len(results),
        "resumed_completed_apps": len(completed_slugs),
        "requested_fields": list(requested_fields or ()),
        "source_only": args.source_only,
        "apps": [
            {
                "app": seed.name,
                "targets": result.get("targets", []),
                "research_status": result.get("validation", {}).get("research_status"),
                "recovery_status": result.get("recovery", {}).get("status", "recorded"),
            }
            for seed, result in results
        ],
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({"recovery_directory": str(output_dir), "completed_apps": len(records)}, indent=2))
    return output_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Append-only focused recovery for already completed quality traces.")
    parser.add_argument("--trace-dir", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument(
        "--fields", nargs="+", choices=("one_liner", "auth_methods", "self_serve_status", "api_surface_types", "api_breadth_notes", "mcp_status", "buildability_verdict", "blocker"),
        help="Optionally recover only these generic weak fields; grounded or non-target fields remain unchanged.",
    )
    parser.add_argument("--source-only", action="store_true", help="Discover/fetch/validate evidence without issuing an LLM request.")
    parser.add_argument("--resume-dir", type=Path, help="Append only missing app records to an interrupted recovery directory.")
    args = parser.parse_args()
    try:
        asyncio.run(main_async(args))
    except ValueError as exc:
        raise SystemExit(f"Recovery selection error: {exc}") from exc


if __name__ == "__main__":
    main()
