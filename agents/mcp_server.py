"""Expose finished Scout100 findings as agent-callable MCP tools over stdio."""
from __future__ import annotations
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mcp.server.fastmcp import FastMCP
from pipeline.analysis import patterns, verification_summary
from pipeline.storage import latest_findings, list_seeds, verification_rows
mcp = FastMCP("Scout100 research findings")

def _db(): return os.getenv("SCOUT100_DB", ROOT / "data/research.db")
def _findings():
    names = {seed.id: seed.name for seed in list_seeds(_db())}
    return [{**f.model_dump(mode="json"), "name": names[f.app_id]} for _, f in latest_findings(_db())]

@mcp.tool()
def get_app_record(app_name: str) -> dict | None:
    """Return the latest grounded finding for one app."""
    query = app_name.casefold().strip()
    for finding in _findings():
        if finding["name"].casefold() == query: return finding
    return None

@mcp.tool()
def list_findings(category: str | None = None, self_serve_status: str | None = None, buildability_verdict: str | None = None) -> list[dict]:
    """Filter the latest findings by category, access path, or verdict."""
    return [f for f in _findings() if (not category or f["category"] == category) and (not self_serve_status or f["self_serve_status"] == self_serve_status) and (not buildability_verdict or f["buildability_verdict"] == buildability_verdict)]

@mcp.tool()
def get_pattern_summary() -> dict:
    """Return auth, access, category and blocker aggregates."""
    fs = latest_findings(_db()); return patterns([f for _, f in fs])

@mcp.tool()
def get_verification_report() -> dict:
    """Return field-level independent verification results and mismatch list."""
    return verification_summary(verification_rows(_db()))

if __name__ == "__main__": mcp.run()
