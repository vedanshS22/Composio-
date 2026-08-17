from agents.research_agent import _merge_recovery, normalize_payload
from pipeline.schema import AppSeed


SEED = AppSeed(id=1, name="Example", category="Test", hint_url="https://developer.example.com/docs")
URL = "https://www.example.com/product/overview"


def test_grounded_fields_survive_missing_mcp_and_verdict() -> None:
    finding = normalize_payload(
        SEED,
        {
            "one_liner": "Example is a customer platform for managing service requests and team workflows.",
            "auth_methods": ["oauth2"],
            "api_surface_type": "rest",
            "mcp_exists": None,
            "buildability_verdict": None,
            "evidence": [
                {"field": "one_liner", "url": URL},
                {"field": "auth_methods", "url": URL},
                {"field": "api_surface_type", "url": URL},
            ],
        },
        {URL},
        [],
        False,
        source_text_by_url={URL: "OAuth REST API"},
    )
    assert finding.one_liner == "Example is a customer platform for managing service requests and team workflows."
    assert finding.auth_methods == ["oauth2"]
    assert finding.api_surface_type == "rest"
    assert finding.mcp_status == "unknown"
    assert finding.buildability_verdict == "yes_with_caveats"
    assert finding.research_status == "partial"


def test_recovery_merge_cannot_replace_grounded_initial_field() -> None:
    initial = {"one_liner": "Initial value", "evidence": [{"field": "one_liner", "url": URL}]}
    recovery = {
        "one_liner": "Replacement value",
        "api_breadth_notes": "Objects and actions.",
        "evidence": [
            {"field": "one_liner", "url": URL},
            {"field": "api_breadth_notes", "url": URL},
        ],
    }
    merged = _merge_recovery(initial, recovery, ("api_breadth_notes",))
    assert merged["one_liner"] == "Initial value"
    assert merged["api_breadth_notes"] == "Objects and actions."
    assert [item["field"] for item in merged["evidence"]] == ["one_liner", "api_breadth_notes"]


def test_multi_auth_and_api_surfaces_require_item_level_evidence() -> None:
    finding = normalize_payload(
        SEED,
        {
            "one_liner": "Example is a customer platform for managing service requests and team workflows.",
            "auth_methods": ["oauth2", "api_key"],
            "api_surface_types": ["rest", "graphql"],
            "evidence": [
                {"field": "one_liner", "url": URL},
                {"field": "auth_methods", "value": "oauth2", "url": URL},
                {"field": "api_surface_types", "value": "rest", "url": URL},
                {"field": "api_surface_types", "value": "graphql", "url": URL},
            ],
        },
        {URL}, [], False, source_text_by_url={URL: "OAuth REST GraphQL API"},
    )
    assert finding.auth_methods == ["oauth2"]
    assert finding.api_surface_types == ["rest", "graphql"]


def test_malformed_multi_value_items_are_field_level_unknown_not_crash() -> None:
    finding = normalize_payload(
        SEED,
        {
            "one_liner": "Example is a customer platform for managing service requests and team workflows.",
            "auth_methods": [{"value": "oauth2"}],
            "api_surface_types": [{"value": "rest"}],
            "evidence": [{"field": "one_liner", "url": URL}],
        },
        {URL}, [], False, source_text_by_url={URL: "Example platform"},
    )
    assert finding.auth_methods == []
    assert finding.api_surface_types == []
    assert finding.research_status == "partial"
