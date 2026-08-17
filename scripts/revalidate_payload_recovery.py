"""Replay stored LLM payloads through the current evidence normalizer.

This command is deliberately provider-free: it refetches the original,
already-audited source URLs through Composio, then validates the persisted raw
payload again.  It repairs demonstrated parser/evidence defects without
spending another LLM call or replacing any prior trace.
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
from agents.research_agent import _mcp_mention_sources, fetch_sources, normalize_payload
from pipeline.env_loader import load_dotenv
from pipeline.quality_orchestration import run_isolated_batch
from pipeline.quality_recovery import merge_quality_recovery, targets_for
from pipeline.schema import AppFinding, AppSeed
from pipeline.storage import list_seeds
from scripts.recover_completed_findings import completed_records, validation_output
from scripts.run_quality_research import _slug


def _stored_payload(record: dict) -> dict:
    recovery = record.get("recovery") if isinstance(record.get("recovery"), dict) else {}
    payload = recovery.get("payload")
    if not isinstance(payload, dict):
        llm = record.get("llm") if isinstance(record.get("llm"), dict) else {}
        payload = llm.get("payload")
    return payload if isinstance(payload, dict) else {"evidence": []}


async def revalidate_one(seed: AppSeed, record: dict) -> dict:
    existing = AppFinding.model_validate(record["validation"]["finding"])
    recovery = record.get("recovery") if isinstance(record.get("recovery"), dict) else {}
    urls = [url for url in recovery.get("sources", []) if isinstance(url, str)]
    targets = tuple(record.get("targets", ())) or targets_for(existing)
    result = {"app": seed.name, "source_trace": record.get("app"), "targets": list(targets)}
    if not urls:
        result["revalidation"] = {"status": "no_stored_sources", "llm_called": False}
        result["validation"] = validation_output(existing)
        return result
    client = await asyncio.to_thread(ComposioResearchMCP)
    sources, fetch_blockers = await fetch_sources(urls, client)
    if not sources:
        result["revalidation"] = {"status": "fetch_failure", "llm_called": False, "sources": urls, "fetch_blockers": fetch_blockers}
        result["validation"] = validation_output(existing)
        return result
    payload = _stored_payload(record)
    normalized = normalize_payload(
        seed, payload, {url for url, _ in sources}, list(fetch_blockers), False,
        _mcp_mention_sources(sources), dict(sources),
    )
    merged = merge_quality_recovery(existing, normalized, targets)
    result["revalidation"] = {
        "status": "revalidated", "llm_called": False,
        "sources": [url for url, _ in sources], "fetch_blockers": fetch_blockers,
        "payload_fields": sorted(payload),
    }
    result["validation"] = validation_output(merged)
    return result


async def main_async(args: argparse.Namespace) -> Path:
    load_dotenv()
    source_dir = args.trace_dir.resolve()
    if not source_dir.is_dir():
        raise ValueError(f"Trace directory does not exist: {source_dir}")
    seeds = list_seeds(ROOT / "data" / "research.db")
    records = completed_records(source_dir, seeds)
    output_dir = ROOT / "data" / "logs" / "completed_recovery" / datetime.now(UTC).strftime("payload_revalidation_%Y%m%dT%H%M%SZ")
    output_dir.mkdir(parents=True, exist_ok=False)

    by_id = {seed.id: record for seed, record in records}

    async def worker(seed: AppSeed) -> dict:
        return await revalidate_one(seed, by_id[seed.id])

    async def persist(seed: AppSeed, result: dict) -> None:
        (output_dir / f"{_slug(seed)}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")

    results = await run_isolated_batch([seed for seed, _ in records], worker, concurrency=args.concurrency, on_complete=persist)
    (output_dir / "summary.json").write_text(json.dumps({
        "source_trace_directory": str(source_dir), "completed_apps": len(results), "llm_called": False,
    }, indent=2), encoding="utf-8")
    print(json.dumps({"revalidation_directory": str(output_dir), "completed_apps": len(results), "llm_called": False}, indent=2))
    return output_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay stored research payloads through current validation without calling an LLM.")
    parser.add_argument("--trace-dir", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, default=2)
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
