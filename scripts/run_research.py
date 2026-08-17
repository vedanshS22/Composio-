from __future__ import annotations
import argparse, asyncio, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pipeline.env_loader import load_dotenv
from pipeline.orchestrator import run_research
from pipeline.storage import init_db

load_dotenv()

def main() -> None:
    parser = argparse.ArgumentParser(description="Run the evidence-first Scout100 research agent.")
    parser.add_argument("--apps", help="Comma-separated app names; default is every seed app")
    parser.add_argument("--pass-number", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=5)
    args = parser.parse_args()
    db = ROOT / "data/research.db"; init_db(db)
    names = {name.strip().lower() for name in args.apps.split(",")} if args.apps else None
    count = asyncio.run(run_research(db, names, args.pass_number, concurrency=args.concurrency))
    print(f"Persisted {count} append-only pass-{args.pass_number} findings.")
if __name__ == "__main__": main()
