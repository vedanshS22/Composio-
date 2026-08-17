"""Convenient quality-gate entry point for the latest generic trace run."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.quality_gate import evaluate


def latest_trace() -> Path:
    root = ROOT / "data" / "logs" / "quality_traces"
    candidates = [path for path in root.glob("quality_*") if path.is_dir()]
    if not candidates:
        raise ValueError("No quality trace exists. Run scripts/run_quality_research.py first.")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate the latest or specified generic research trace.")
    parser.add_argument("--trace-dir", type=Path, help="Optional quality-trace directory")
    args = parser.parse_args()
    trace_dir = (args.trace_dir or latest_trace()).resolve()
    result = evaluate(trace_dir)
    (trace_dir / "quality_gate.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
