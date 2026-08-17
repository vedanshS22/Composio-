"""Append a corrective research pass only for current unresolved findings."""
from __future__ import annotations
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pipeline.orchestrator import run_research
from pipeline.storage import latest_findings, list_seeds

async def main_async() -> None:
    db = ROOT / "data/research.db"
    unresolved_ids = {finding.app_id for _, finding in latest_findings(db) if finding.needs_human_review or finding.self_serve_status == "unknown" or finding.api_surface_type == "unknown"}
    seeds = {seed.id: seed for seed in list_seeds(db)}
    names = {seeds[app_id].name.lower() for app_id in unresolved_ids}
    count = await run_research(db, names, pass_number=3, concurrency=1)
    print(f"Appended pass-3 findings for {count} unresolved apps.")

if __name__ == "__main__": asyncio.run(main_async())
