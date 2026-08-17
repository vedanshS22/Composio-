from __future__ import annotations
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pipeline.schema import AppSeed
from pipeline.storage import seed_apps


def load_seed_input(path: Path) -> list[AppSeed]:
    """Read a generic CSV with name, website/hint_url, category, and optional id."""
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError(f"{path} contains no companies")
    seen_names: set[str] = set()
    seen_ids: set[int] = set()
    seeds: list[AppSeed] = []
    for row_number, row in enumerate(rows, start=1):
        name = (row.get("name") or "").strip()
        category = (row.get("category") or "").strip()
        website = (row.get("website") or row.get("hint_url") or "").strip()
        raw_id = (row.get("id") or "").strip()
        app_id = int(raw_id) if raw_id else row_number
        if not name or not category or not website:
            raise ValueError(f"row {row_number} must provide name, website (or hint_url), and category")
        if name.casefold() in seen_names:
            raise ValueError(f"duplicate company name in input: {name}")
        if app_id in seen_ids:
            raise ValueError(f"duplicate id in input: {app_id}")
        seen_names.add(name.casefold())
        seen_ids.add(app_id)
        seeds.append(AppSeed(id=app_id, name=name, category=category, hint_url=website))
    return seeds


def main() -> None:
    parser = __import__("argparse").ArgumentParser(description="Load companies from a generic CSV input file.")
    parser.add_argument("--input", type=Path, default=ROOT / "data/apps.csv", help="CSV with name, website (or hint_url), category, and optional id")
    args = parser.parse_args()
    seeds = load_seed_input(args.input)
    total = seed_apps(ROOT / "data/research.db", seeds)
    print(f"Loaded {len(seeds)} companies from {args.input}; database contains {total} companies.")
if __name__ == "__main__": main()
