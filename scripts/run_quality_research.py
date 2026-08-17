"""Generic, append-only, non-persisting quality-research trace runner."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.composio_mcp import ComposioResearchMCP
from pipeline.env_loader import load_dotenv
from pipeline.quality_orchestration import resolve_seed_selection, run_isolated_batch
from pipeline.quality_recovery import recover_quality, targets_for
from pipeline.schema import AppFinding, AppSeed
from pipeline.storage import list_seeds
from scripts.build_trace_report import build_trace_report
from scripts.trace_research import trace

SAMPLE_CONFIG = ROOT / "data" / "verification20_seed_ids.json"


def _sample_ids(name: str) -> list[int]:
    if name != "verification20":
        raise ValueError("unknown sample; supported sample: verification20")
    payload = json.loads(SAMPLE_CONFIG.read_text(encoding="utf-8"))
    return payload["seed_ids"]


def _completed_seed_ids(trace_dir: Path, seeds: list[AppSeed]) -> set[int]:
    """Resolve completed append-only trace records without naming any apps."""
    by_name = {seed.name.casefold(): seed.id for seed in seeds}
    completed: set[int] = set()
    for path in trace_dir.glob("*.json"):
        if path.name == "summary.json":
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        app = payload.get("app")
        if isinstance(app, str) and app.casefold() in by_name:
            completed.add(by_name[app.casefold()])
    return completed


TRANSIENT_MARKERS = ("connection", "timeout", "429", "500", "502", "503", "504", "taskgroup")

def _sanitize_error(exc: Exception | str) -> str:
    text = str(exc)
    text = re.sub(r"(Bearer\s+|sk-or-[\w-]+|gsk_[\w-]+|ak_)[^\s,;]+", r"\1[REDACTED]", text)
    return " ".join(text.split())[:600]


def _is_transient(result: dict) -> bool:
    parts = [
        str(result.get("run_failure", {}).get("error", "")),
        str(result.get("llm_or_validation", {}).get("error", "")),
        str(result.get("quality_recovery", {}).get("error", "")),
    ]
    return any(marker in " ".join(parts).lower() for marker in TRANSIENT_MARKERS)


async def research_with_quality(seed: AppSeed, max_llm_calls: int | None = None) -> dict:
    """Run one generic trace plus bounded, field-only recovery attempts.

    Each recovery redraws sources for only the still-missing fields and merges
    only stronger evidence.  The app-level cap prevents a difficult public
    site from creating an unbounded paid-call loop.
    """
    limit = max_llm_calls or int(os.getenv("LLM_MAX_CALLS_PER_APP", "4"))
    if limit < 1:
        raise ValueError("max_llm_calls must be at least 1")
    try:
        # One hosted MCP session is reused by the initial trace and every
        # recovery for this AppSeed.
        client = await asyncio.to_thread(ComposioResearchMCP)
        result = await trace(seed.name, client=client)
    except Exception as exc:
        return {"app": seed.name, "category": seed.category, "run_failure": {"status": "failure", "error": _sanitize_error(exc)}}
    validation = result.get("validation")
    if not isinstance(validation, dict) or not isinstance(validation.get("finding"), dict):
        return result
    if validation.get("execution_failure"):
        return result
    existing = AppFinding.model_validate(validation["finding"])
    initial_calls = 1 + int(bool(isinstance(result.get("llm"), dict) and result["llm"].get("fallback_used")))
    pending = set(targets_for(existing))
    # Cost contract: one compact initial extraction and, at most, one compact
    # field-only recovery call.  All remaining weak fields share that recovery
    # context; a completed field is never included in it.
    recovery_batches = [tuple(field for field in targets_for(existing) if field in pending)] if pending else []
    recovery_attempts: list[dict] = []
    for number, fields in enumerate(recovery_batches, start=initial_calls + 1):
        if number > limit or existing.research_status == "grounded":
            break
        try:
            quality = await recover_quality(seed, existing, client, fields=fields)
            quality["attempt"] = number
            recovery_attempts.append(quality)
            resolved = quality.get("finding")
            if not isinstance(resolved, dict):
                break
            existing = AppFinding.model_validate(resolved)
            validation.update({
                "research_status": existing.research_status,
                "evidence_fields": [item.field for item in existing.evidence],
                "research_blockers": existing.research_blockers,
                "buildability_verdict": existing.buildability_verdict,
                "finding": resolved,
            })
            if not quality.get("targets"):
                break
        except Exception as exc:
            recovery_attempts.append({"attempt": number, "status": "failure", "error": f"{type(exc).__name__}: {_sanitize_error(exc)}"})
    if recovery_attempts:
        result["quality_recovery_attempts"] = recovery_attempts
        result["quality_recovery"] = recovery_attempts[-1]
    return result


def _slug(seed: AppSeed) -> str:
    return "".join(char.lower() if char.isalnum() else "_" for char in seed.name).strip("_")


async def main_async(args: argparse.Namespace) -> Path:
    load_dotenv()
    db = ROOT / "data" / "research.db"
    seeds = list_seeds(db)
    if args.remaining_from_trace:
        trace_dir = args.remaining_from_trace.resolve()
        if not trace_dir.is_dir():
            raise ValueError(f"completed trace directory does not exist: {trace_dir}")
        completed_ids = _completed_seed_ids(trace_dir, seeds)
        selection = [seed for seed in seeds if seed.id not in completed_ids]
    else:
        selection = resolve_seed_selection(
            seeds, app_names=args.apps,
            sample_ids=_sample_ids(args.sample) if args.sample else None,
            all_apps=args.all,
        )
    run_dir = ROOT / "data" / "logs" / "quality_traces" / datetime.now(UTC).strftime("quality_%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=False)
    live_output = args.live_output.resolve() if args.live_output else None
    async def persist_trace(seed: AppSeed, result: dict) -> None:
        # Persist every completed app immediately. A later provider failure can
        # never erase already-auditable traces from this append-only run.
        (run_dir / f"{_slug(seed)}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        if live_output:
            build_trace_report(run_dir, live_output)

    async def worker(seed: AppSeed) -> dict:
        return await research_with_quality(seed, max_llm_calls=args.max_llm_calls_per_app)
    results = await run_isolated_batch(selection, worker, concurrency=args.concurrency, on_complete=persist_trace)
    summary = []
    for seed, result in results:
        validation = result.get("validation", result.get("llm_or_validation", {}))
        summary.append({
            "app": seed.name, "category": seed.category,
            "research_status": validation.get("research_status"),
            "run_failure": result.get("run_failure", {}).get("error"),
            "quality_recovery": result.get("quality_recovery", {}).get("status", "recorded"),
        })
    (run_dir / "summary.json").write_text(json.dumps({"apps": [seed.name for seed in selection], "summary": summary}, indent=2), encoding="utf-8")
    print(json.dumps({"trace_directory": str(run_dir), "summary": summary}, indent=2))
    return run_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run generic, non-persisting quality research on seeded apps.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--apps", help="Comma-separated canonical seed names")
    group.add_argument("--sample", help="Named fixed seed sample (verification20)")
    group.add_argument("--all", action="store_true", help="Every app in the fixed seed dataset")
    group.add_argument("--remaining-from-trace", type=Path, help="Every seed without a trace in this append-only completed-run directory")
    parser.add_argument("--concurrency", type=int, default=1, help="Independent app traces to run concurrently (default: 1)")
    parser.add_argument("--max-llm-calls-per-app", type=int, default=int(os.getenv("LLM_MAX_CALLS_PER_APP", "4")), help="Hard cap for initial extraction plus generic field recovery (default: 4)")
    parser.add_argument("--live-output", type=Path, help="Rebuild this static 100-app report after every completed trace.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        asyncio.run(main_async(args))
    except ValueError as exc:
        raise SystemExit(f"Selection error: {exc}") from exc


if __name__ == "__main__":
    main()
