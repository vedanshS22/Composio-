from pipeline.reviewer_view import reviewer_row
from pipeline.schema import AppFinding, Evidence


def test_reviewer_row_keeps_unknown_field_level_and_marks_poc_mcp() -> None:
    finding = AppFinding(
        app_id=1, category="CRM and Sales", one_liner="A documented platform.",
        auth_methods=["oauth2", "api_key"], self_serve_status="unknown",
        api_surface_type="rest", api_breadth_notes="Objects, events, workflows, and files.",
        mcp_exists=True, mcp_status="official", mcp_notes="A proof of concept MCP server.",
        buildability_verdict="yes", confidence=0.8, research_status="partial",
        evidence=[
            Evidence(field="one_liner", url="https://example.com/description"),
            Evidence(field="auth_methods", url="https://example.com/auth", note="OAuth2 and API Key authentication."),
            Evidence(field="api_surface_type", url="https://example.com/api"),
            Evidence(field="api_breadth_notes", url="https://example.com/api"),
            Evidence(field="mcp_status", url="https://example.com/mcp"),
            Evidence(field="buildability_verdict", url="https://example.com/api"),
        ],
    )
    row = reviewer_row("Example", finding)
    assert row.category == "CRM & Sales"
    assert row.auth == "OAuth2 · API Key"
    assert row.access == "Unknown"
    assert row.mcp == "Proof-of-concept / experimental MCP"
    assert row.buildability == "Yes — with caveats"
    assert row.unresolved_fields == ["Access"]
    assert [item["label"] for item in row.evidence] == ["Description", "Auth", "API", "MCP"]


def test_projection_keeps_access_and_breadth_already_validated_by_the_finding() -> None:
    finding = AppFinding(
        app_id=1, category="CRM and Sales", self_serve_status="self_serve_trial",
        api_surface_type="rest", api_surface_types=["rest"], api_surface_breadth="broad",
        confidence=0.4, research_status="partial",
        evidence=[
            Evidence(field="self_serve_status", url="https://example.com/signup", note="Trial available."),
            Evidence(field="api_surface_types", value="rest", url="https://example.com/api"),
            Evidence(field="api_surface_breadth", url="https://example.com/api", note="Many API resources."),
        ],
    )
    row = reviewer_row("Example", finding)
    assert row.access == "Self-serve trial"
    assert row.api_surface == "REST\nBreadth: Broad"
    assert "Access" not in row.unresolved_fields
    assert "API breadth" not in row.unresolved_fields


def test_unattempted_row_is_not_given_a_repeated_credential_product_caveat() -> None:
    finding = AppFinding(
        app_id=1, category="CRM and Sales", confidence=0,
        research_status="unresolved", research_blockers=["no_validated_finding"],
    )
    row = reviewer_row("Example", finding)
    assert row.main_blocker_or_caveat == "Research pending — no product conclusion"


def test_unresolved_access_caveat_shows_only_evidenced_credential_requirements() -> None:
    finding = AppFinding(
        app_id=1, category="CRM and Sales", auth_methods=["oauth2", "api_key"],
        confidence=0.2, research_status="partial",
        evidence=[
            Evidence(field="auth_methods", value="oauth2", url="https://example.com/oauth"),
            Evidence(field="auth_methods", value="api_key", url="https://example.com/api-key"),
        ],
    )
    row = reviewer_row("Example", finding)
    assert row.main_blocker_or_caveat == "Credential access unresolved — documented authentication: OAuth2 · API Key"
