"""Generic focused-quality recovery built on the unchanged research engine."""
from __future__ import annotations

from agents.composio_mcp import ComposioResearchMCP
from agents.research_agent import (
    _extract_payload,
    _is_product_description,
    _mcp_mention_sources,
    discover_sources,
    fetch_sources,
    normalize_payload,
)
from pipeline.schema import AppFinding, AppSeed


QUALITY_FIELDS = (
    "one_liner", "auth_methods", "self_serve_status", "api_surface_types",
    "api_breadth_notes", "mcp_status", "buildability_verdict",
)


def targets_for(finding: AppFinding) -> tuple[str, ...]:
    """Find weak fields without rerunning fields that are already grounded."""
    targets: list[str] = []
    if not finding.one_liner or not _is_product_description(finding.one_liner):
        targets.append("one_liner")
    # Auth recovery is additive when the current methods lack per-value
    # provenance.  A grounded, evidenced auth field is retained; discovering
    # hypothetical extra methods is not a reason to re-run it on every app.
    auth_evidence = [item for item in finding.evidence if item.field == "auth_methods"]
    auth_complete = bool(finding.auth_methods) and all(
        any(item.value == method for item in auth_evidence)
        or (len(finding.auth_methods) == 1 and any(item.value is None for item in auth_evidence))
        for method in finding.auth_methods
    )
    if not auth_complete:
        targets.append("auth_methods")
    if finding.self_serve_status == "unknown":
        targets.append("self_serve_status")
    api_urls = [str(item.url).lower() for item in finding.evidence if item.field in {"api_surface_types", "api_surface_type", "api_breadth_notes"}]
    if not finding.api_breadth_notes or (not finding.api_surface_types and finding.api_surface_type == "unknown") or any("oauth" in url for url in api_urls):
        targets.extend(("api_surface_types", "api_breadth_notes"))
    if finding.mcp_status == "unknown":
        targets.append("mcp_status")
    if finding.buildability_verdict is None:
        targets.extend(("buildability_verdict", "blocker"))
    elif not finding.blocker or (
        finding.blocker == "Credential access unresolved" and finding.self_serve_status != "unknown"
    ):
        # A resolved credential path may make the older generic caveat stale.
        # Re-evaluate the verdict and its product caveat together, never from
        # an infrastructure failure.
        targets.extend(("buildability_verdict", "blocker"))
    return tuple(dict.fromkeys(targets))


def _resolved(field: str, finding: AppFinding) -> bool:
    value = getattr(finding, field)
    return value is not None and value != "unknown" and value != []


def merge_quality_recovery(existing: AppFinding, recovered: AppFinding, targets: tuple[str, ...]) -> AppFinding:
    """Append evidence and accept only actual field recoveries; never erase data."""
    data = existing.model_dump(mode="python")
    accepted_fields = {item.field for item in recovered.evidence if item.field in targets}
    if "auth_methods" in targets and "auth_methods" in accepted_fields and recovered.auth_methods:
        data["auth_methods"] = list(dict.fromkeys([*existing.auth_methods, *recovered.auth_methods]))
    for field in ("one_liner", "self_serve_status", "api_surface_type", "api_breadth_notes", "api_surface_breadth", "api_surface_summary", "mcp_status", "buildability_verdict", "blocker"):
        if field in targets and field in accepted_fields and _resolved(field, recovered):
            data[field] = getattr(recovered, field)
    if "api_surface_types" in targets and "api_surface_types" in accepted_fields and recovered.api_surface_types:
        data["api_surface_types"] = list(dict.fromkeys([*existing.api_surface_types, *recovered.api_surface_types]))
        data["api_surface_type"] = recovered.api_surface_type
    if "mcp_status" in targets and "mcp_status" in accepted_fields and recovered.mcp_exists is not None:
        data["mcp_exists"] = recovered.mcp_exists
        data["mcp_notes"] = recovered.mcp_notes
    evidence = [*existing.evidence]
    for item in recovered.evidence:
        if item.field in accepted_fields and not any(old.field == item.field and old.url == item.url for old in evidence):
            evidence.append(item)
    data["evidence"] = evidence
    resolved_targets = {field for field in targets if _resolved(field, AppFinding.model_construct(**data))}
    blockers = [
        item for item in existing.research_blockers
        if not any(item == f"no grounded evidence for {field}" for field in resolved_targets)
    ]
    blockers.extend(
        item for item in recovered.research_blockers
        if not item.startswith("no grounded evidence for") and item not in blockers
    )
    data["research_blockers"] = blockers
    # Status and blocker calculation remains fail-closed and field-level.
    unresolved = [not _resolved(field, AppFinding.model_construct(**data)) for field in QUALITY_FIELDS]
    data["confidence"] = round((len(QUALITY_FIELDS) - sum(unresolved)) / len(QUALITY_FIELDS), 2)
    data["research_status"] = "grounded" if not any(unresolved) and not blockers else ("partial" if any(not item for item in unresolved) else "unresolved")
    data["needs_human_review"] = data["research_status"] != "grounded"
    return AppFinding.model_validate(data)


async def recover_quality(
    seed: AppSeed,
    existing: AppFinding,
    client: ComposioResearchMCP,
    fields: tuple[str, ...] | None = None,
    allow_llm: bool = True,
) -> dict:
    """Run a focused recovery trace for an arbitrary seeded app."""
    targets = fields or targets_for(existing)
    if not targets:
        return {"targets": [], "finding": existing.model_dump(mode="json"), "status": "not_needed"}
    urls, discovery_blockers = await discover_sources(seed, client, targets)
    sources, fetch_blockers = await fetch_sources(urls, client)
    if not sources:
        return {
            "targets": list(targets), "sources": urls,
            "recovery_failure": "No focused source could be fetched.",
            "finding": existing.model_dump(mode="json"),
        }
    source_derived = normalize_payload(
        seed, {"evidence": []}, {url for url, _ in sources}, discovery_blockers + fetch_blockers,
        False, _mcp_mention_sources(sources), dict(sources),
    )
    after_source_derivation = merge_quality_recovery(existing, source_derived, targets)
    remaining = tuple(field for field in targets if field in targets_for(after_source_derivation))
    if not remaining:
        return {
            "targets": list(targets), "sources": [url for url, _ in sources],
            "status": "source_derived",
            "source_discovery_blockers": discovery_blockers, "fetch_blockers": fetch_blockers,
            "finding": after_source_derivation.model_dump(mode="json"),
        }
    if not allow_llm:
        return {
            "targets": list(targets), "remaining_llm_targets": list(remaining),
            "sources": [url for url, _ in sources],
            "status": "source_only_pending",
            "source_discovery_blockers": discovery_blockers, "fetch_blockers": fetch_blockers,
            "finding": after_source_derivation.model_dump(mode="json"),
        }
    payload, usage = await _extract_payload(seed, sources, None, remaining, capture_usage=True)
    recovered = normalize_payload(
        seed, payload, {url for url, _ in sources}, discovery_blockers + fetch_blockers,
        False, _mcp_mention_sources(sources), dict(sources),
    )
    merged = merge_quality_recovery(after_source_derivation, recovered, remaining)
    return {
        "targets": list(targets), "remaining_llm_targets": list(remaining), "sources": [url for url, _ in sources],
        "llm_usage": usage,
        "source_discovery_blockers": discovery_blockers, "fetch_blockers": fetch_blockers,
        # Keep the compact raw response for append-only diagnosis.  It is
        # needed to distinguish model omission from a later normalization
        # rejection; it contains no credentials.
        "payload": payload,
        "recovered": recovered.model_dump(mode="json"), "finding": merged.model_dump(mode="json"),
    }
