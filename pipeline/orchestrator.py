from __future__ import annotations

import asyncio
from pathlib import Path

from agents.research_agent import research_app
from agents.composio_mcp import ComposioResearchMCP
from pipeline.storage import connect, create_run, finish_run, list_seeds, save_finding


async def run_research(db_path: str | Path, names: set[str] | None = None, pass_number: int = 1, hints: dict[int, str] | None = None, concurrency: int = 5) -> int:
    seeds = list_seeds(db_path, names)
    composio_client = ComposioResearchMCP()
    semaphore = asyncio.Semaphore(concurrency)
    async def one(seed):
        async with semaphore:
            return seed, await research_app(seed, (hints or {}).get(seed.id), composio_client=composio_client)
    results = await asyncio.gather(*(one(seed) for seed in seeds))
    with connect(db_path) as conn:
        run_id = create_run(conn, pass_number, f"research pass {pass_number}")
        for _, finding in results:
            save_finding(conn, run_id, pass_number, finding)
        finish_run(conn, run_id)
    return len(results)
