"""Read-only helper for inspecting generic Access evidence in fetched pages."""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agents.composio_mcp import ComposioResearchMCP
from pipeline.env_loader import load_dotenv


TERMS = ("free", "charge", "fee", "pricing", "review", "approval", "verification", "business account", "developer", "credential", "signup")


async def main_async(urls: list[str]) -> None:
    load_dotenv()
    client = await asyncio.to_thread(ComposioResearchMCP)
    results = await asyncio.gather(*(client.fetch_urls_content([url]) for url in urls), return_exceptions=True)
    for url, result in zip(urls, results):
        print(f"URL={url}")
        if isinstance(result, Exception):
            print(f"FETCH_ERROR={type(result).__name__}")
            continue
        pages, _ = result
        text = pages[0][1] if pages else ""
        sentences = re.split(r"(?<=[.!?])\s+", " ".join(text.split()))
        matches = [sentence for sentence in sentences if any(term in sentence.lower() for term in TERMS)]
        print("\n".join(matches[:12])[:2_000])


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch and display Access-relevant text from supplied sources.")
    parser.add_argument("urls", nargs="+")
    args = parser.parse_args()
    asyncio.run(main_async(args.urls))


if __name__ == "__main__":
    main()
