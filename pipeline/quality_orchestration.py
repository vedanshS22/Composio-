"""Generic, non-persisting orchestration helpers for quality research traces.

This module intentionally does not implement research.  It resolves fixed seed
selections and isolates app-level failures while the evidence-first agent keeps
ownership of discovery, fetching, extraction, validation, and recovery.
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from pipeline.schema import AppSeed


def _sanitized_error(exc: Exception) -> str:
    text = re.sub(r"(Bearer\s+|sk-or-[\w-]+|gsk_[\w-]+|ak_)[^\s,;]+", r"\1[REDACTED]", str(exc))
    return " ".join(text.split())[:600]


def resolve_seed_selection(
    seeds: Iterable[AppSeed],
    *,
    app_names: str | None = None,
    sample_ids: Iterable[int] | None = None,
    all_apps: bool = False,
) -> list[AppSeed]:
    """Resolve a CLI selection against canonical, fixed seed records."""
    choices = [seed for seed in seeds]
    if sum((bool(app_names), sample_ids is not None, all_apps)) != 1:
        raise ValueError("choose exactly one of --apps, --sample, or --all")
    if all_apps:
        return choices
    if sample_ids is not None:
        wanted = set(sample_ids)
        result = [seed for seed in choices if seed.id in wanted]
        missing = wanted - {seed.id for seed in result}
        if missing:
            raise ValueError(f"sample references missing seed IDs: {', '.join(map(str, sorted(missing)))}")
        return result
    requested = [name.strip() for name in (app_names or "").split(",") if name.strip()]
    if not requested:
        raise ValueError("--apps must contain at least one seeded app name")
    by_name = {seed.name.casefold(): seed for seed in choices}
    unknown = [name for name in requested if name.casefold() not in by_name]
    if unknown:
        raise ValueError(f"unknown seeded app name(s): {', '.join(unknown)}")
    # Keep request order but never research the same seed twice.
    result: list[AppSeed] = []
    seen_ids: set[int] = set()
    for name in requested:
        seed = by_name[name.casefold()]
        if seed.id not in seen_ids:
            result.append(seed)
            seen_ids.add(seed.id)
    return result


async def run_isolated_batch(
    seeds: Iterable[AppSeed],
    worker: Callable[[AppSeed], Awaitable[dict[str, Any]]],
    *,
    concurrency: int = 1,
    on_complete: Callable[[AppSeed, dict[str, Any]], Awaitable[None] | None] | None = None,
) -> list[tuple[AppSeed, dict[str, Any]]]:
    """Run each app independently; one failure always becomes that app's trace."""
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    semaphore = asyncio.Semaphore(concurrency)

    async def one(seed: AppSeed) -> tuple[AppSeed, dict[str, Any]]:
        async with semaphore:
            try:
                result = await worker(seed)
            except Exception as exc:  # deliberate per-app audit isolation
                result = {
                    "app": seed.name,
                    "category": seed.category,
                    "run_failure": {"status": "failure", "error": f"{type(exc).__name__}: {_sanitized_error(exc)}"},
                }
            if on_complete:
                callback_result = on_complete(seed, result)
                if asyncio.iscoroutine(callback_result):
                    await callback_result
            return seed, result

    return await asyncio.gather(*(one(seed) for seed in seeds))
