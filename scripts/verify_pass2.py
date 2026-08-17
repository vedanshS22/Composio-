"""Re-check only the apps that actually received an append-only pass-2 finding."""
from __future__ import annotations
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agents.verifier_agent import verify_finding
from pipeline.storage import all_findings, connect, list_seeds, save_verification

async def main_async() -> None:
    db = ROOT / "data/research.db"
    latest_per_app = {}
    for finding_id, number, finding in all_findings(db):
        if number == 2 and (finding.app_id not in latest_per_app or finding_id > latest_per_app[finding.app_id][0]):
            latest_per_app[finding.app_id] = (finding_id, finding)
    pass2 = list(latest_per_app.values())
    seeds = {seed.id: seed for seed in list_seeds(db)}
    with connect(db) as conn:
        for finding_id, prior in pass2:
            _, results = await verify_finding(seeds[prior.app_id], prior)
            for result in results:
                save_verification(conn, finding_id, "independent_docs_agent_pass2", result)
    print(f"Re-verified {len(pass2)} pass-2 apps.")

if __name__ == "__main__": asyncio.run(main_async())
