import asyncio
import re
from pathlib import Path

import pytest

from pipeline.quality_orchestration import resolve_seed_selection, run_isolated_batch
from pipeline.quality_recovery import merge_quality_recovery, targets_for
from pipeline.schema import AppFinding, AppSeed, Evidence
from pipeline.storage import list_seeds


SEEDS = [
    AppSeed(id=1, name="Example One", category="Test", hint_url="https://one.example/docs"),
    AppSeed(id=2, name="Example Two", category="Test", hint_url="https://two.example/docs"),
]
URL = "https://example.com/docs"


def finding(**updates):
    base = {
        "app_id": 1, "category": "Test",
        "one_liner": "Example One is a customer platform for managing service requests and team workflows.",
        "auth_methods": ["oauth2"], "self_serve_status": "self_serve_free",
        "api_surface_type": "rest", "api_breadth_notes": "Users, tickets, events, files, and workflows.",
        "mcp_exists": True, "mcp_status": "official", "mcp_notes": "Documented MCP server.",
        "buildability_verdict": "yes", "confidence": 1.0, "research_status": "grounded",
        "evidence": [Evidence(field=field, url=URL, note="OAuth API credential developer access") for field in ("one_liner", "auth_methods", "self_serve_status", "api_surface_type", "api_breadth_notes", "mcp_status", "buildability_verdict")],
    }
    base.update(updates)
    return AppFinding(**base)


def test_arbitrary_app_list_resolves_against_seed_names() -> None:
    selected = resolve_seed_selection(SEEDS, app_names="Example Two,Example One")
    assert [seed.name for seed in selected] == ["Example Two", "Example One"]


def test_unknown_app_name_is_rejected_cleanly() -> None:
    with pytest.raises(ValueError, match="unknown seeded app name"):
        resolve_seed_selection(SEEDS, app_names="Not Seeded")


def test_sample_selection_uses_seed_ids() -> None:
    selected = resolve_seed_selection(SEEDS, sample_ids=[2])
    assert [seed.name for seed in selected] == ["Example Two"]


def test_one_app_failure_does_not_stop_the_next() -> None:
    async def worker(seed):
        if seed.id == 1:
            raise RuntimeError("simulated failure")
        return {"app": seed.name, "validation": {"research_status": "grounded"}}

    results = asyncio.run(run_isolated_batch(SEEDS, worker))
    assert results[0][1]["run_failure"]["status"] == "failure"
    assert results[1][1]["validation"]["research_status"] == "grounded"


def test_focused_recovery_targets_are_app_agnostic_and_grounded_fields_survive() -> None:
    existing = finding(one_liner=None, self_serve_status="unknown", research_status="partial")
    assert targets_for(existing)[:2] == ("one_liner", "self_serve_status")
    recovered = finding(
        app_id=1,
        one_liner="Example One is a customer platform that helps teams manage service requests and workflows.",
        auth_methods=["api_key"], self_serve_status="self_serve_free",
    )
    merged = merge_quality_recovery(existing, recovered, targets_for(existing))
    assert merged.one_liner == recovered.one_liner
    assert merged.auth_methods == ["oauth2"]
    assert merged.api_breadth_notes == existing.api_breadth_notes


def test_access_only_recovery_preserves_every_other_grounded_value() -> None:
    existing = finding(self_serve_status="unknown", research_status="partial")
    recovered = finding(
        self_serve_status="self_serve_paid",
        one_liner="Weaker replacement that must not be used.",
        auth_methods=["api_key"],
        api_surface_type="graphql",
        buildability_verdict="no",
        evidence=[Evidence(field="self_serve_status", url=URL, note="A paid plan is required for developer API access.")],
    )
    merged = merge_quality_recovery(existing, recovered, ("self_serve_status",))
    assert merged.self_serve_status == "self_serve_paid"
    assert merged.one_liner == existing.one_liner
    assert merged.auth_methods == existing.auth_methods
    assert merged.api_surface_type == existing.api_surface_type
    assert merged.buildability_verdict == existing.buildability_verdict


def test_production_quality_modules_have_no_pilot_app_branches() -> None:
    root = Path(__file__).resolve().parents[1]
    source = "\n".join((root / path).read_text(encoding="utf-8") for path in (
        "agents/research_agent.py", "pipeline/quality_orchestration.py", "pipeline/quality_recovery.py",
        "scripts/trace_research.py", "scripts/run_quality_research.py",
    ))
    for name in ("Salesforce", "HubSpot", "Slack", "Twilio", *(seed.name for seed in list_seeds(root / "data/research.db"))):
        assert not re.search(rf"(?:==|!=)\s*['\"]{re.escape(name)}['\"]", source, flags=re.IGNORECASE)
