"""Run non-persisting end-to-end evidence traces for the four incident proof apps."""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pipeline.env_loader import load_dotenv
from scripts.trace_research import trace

load_dotenv()
APPS = ("Salesforce", "HubSpot", "Slack", "Twilio")

async def main_async() -> None:
    # Each pilot is audit material. Never let a later failed retry replace an
    # earlier trace that may contain grounded evidence.
    run_id = datetime.now(UTC).strftime("run_%Y%m%dT%H%M%SZ")
    output_dir = ROOT / "data/logs/proof_traces" / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = []
    for app in APPS:
        try:
            result = await trace(app)
        except Exception as exc:
            # An app-level transport/setup failure is itself an auditable pilot
            # result. It must not stop later proof apps from running.
            result = {"app": app, "run_failure": {"status": "failure", "error": f"{type(exc).__name__}: {exc}"}}
        (output_dir / f"{app.lower().replace(' ', '_')}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        validation = result.get("validation", result.get("llm_or_validation", {}))
        summary.append({"app": app, "discovery": result.get("source_discovery", {}).get("status"), "fetch": result.get("fetch", {}).get("status"), "validation": validation.get("status"), "research_status": validation.get("research_status"), "run_failure": result.get("run_failure", {}).get("error")})
    print(json.dumps({"trace_directory": str(output_dir), "summary": summary}, indent=2))

if __name__ == "__main__": asyncio.run(main_async())
