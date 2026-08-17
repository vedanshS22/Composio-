from __future__ import annotations
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pipeline.schema import AppSeed
from pipeline.storage import seed_apps
def main() -> None:
    with (ROOT / "data/apps_seed.csv").open(encoding="utf-8", newline="") as handle:
        seeds = [AppSeed(id=int(row["id"]), name=row["name"], category=row["category"], hint_url=row["hint_url"]) for row in csv.DictReader(handle)]
    assert len(seeds) == 100, f"Expected 100 seed apps; got {len(seeds)}"
    print(f"Seeded {seed_apps(ROOT / 'data/research.db', seeds)}/100 apps.")
if __name__ == "__main__": main()
