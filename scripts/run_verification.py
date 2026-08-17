from __future__ import annotations
import argparse, asyncio, random, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pipeline.env_loader import load_dotenv
from agents.verifier_agent import verify_finding

load_dotenv()
from pipeline.orchestrator import run_research
from pipeline.storage import connect, create_run, finish_run, latest_findings, list_seeds, save_verification
async def main_async(sample_size: int) -> None:
    db = ROOT / "data/research.db"; latest = latest_findings(db)
    if not latest: raise SystemExit("No findings. Run scripts/seed_db.py and scripts/run_research.py first.")
    by_category = {}
    for finding_id, finding in latest: by_category.setdefault(finding.category, []).append((finding_id, finding))
    per_category = max(1, sample_size // len(by_category)); randomizer = random.Random(100)
    sample = [item for group in by_category.values() for item in randomizer.sample(group, min(per_category, len(group)))]
    seed_map = {seed.id: seed for seed in list_seeds(db)}; mismatch_hints = {}
    with connect(db) as conn:
        run_id = create_run(conn, 1, "independent verification sample")
        for finding_id, prior in sample:
            verified, results = await verify_finding(seed_map[prior.app_id], prior)
            for result in results:
                save_verification(conn, finding_id, "independent_docs_agent", result)
                if not result.match: mismatch_hints[prior.app_id] = f"Independent verifier disagreed on {result.field_name}; re-check official docs."
        finish_run(conn, run_id)
    if mismatch_hints:
        names = {seed_map[app_id].name.lower() for app_id in mismatch_hints}
        await run_research(db, names, pass_number=2, hints=mismatch_hints)
    print(f"Verified {len(sample)} apps; pass-2 queued for {len(mismatch_hints)} apps with mismatches.")
def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--sample-size", type=int, default=20)
    args = parser.parse_args(); asyncio.run(main_async(args.sample_size))
if __name__ == "__main__": main()
