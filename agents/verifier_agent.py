"""Independent verifier: it re-fetches the app docs without seeing pass 1."""
from __future__ import annotations

import json

from agents.research_agent import research_app
from pipeline.schema import AppFinding, AppSeed, RESEARCH_FIELDS, VerificationResult


async def verify_finding(seed: AppSeed, prior: AppFinding | None = None) -> tuple[AppFinding, list[VerificationResult]]:
    # `prior` is intentionally not supplied to the researcher: independent context.
    verified = await research_app(seed, rendered=True)
    if prior is None:
        return verified, []
    comparisons: list[VerificationResult] = []
    for field in RESEARCH_FIELDS:
        before, after = getattr(prior, field), getattr(verified, field)
        comparisons.append(VerificationResult(field_name=field, pipeline_value=json.dumps(before, sort_keys=True), verified_value=json.dumps(after, sort_keys=True), match=before == after, notes="Independent docs re-check; review unresolved fields manually." if verified.needs_human_review else None))
    return verified, comparisons
