import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pipeline.storage import latest_findings
if __name__ == "__main__":
    count = len(latest_findings(ROOT / "data/research.db"))
    print(f"{count}/100 apps have a latest finding")
    raise SystemExit(0 if count == 100 else 1)
