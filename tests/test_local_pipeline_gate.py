"""Local, provider-free contract tests for the generic research pipeline."""
from __future__ import annotations

import asyncio
import json

import pytest

from agents import research_agent
from agents.research_agent import LLMRequestError, LLMResponseFormatError, _compact_sources_for_extraction, _decode_json_object, _expand_source_ids, _extract_payload, _prompt, _relevant_excerpt, discover_sources, fetch_sources, normalize_payload
from pipeline import quality_recovery
from pipeline.quality_recovery import merge_quality_recovery, targets_for
from pipeline.reviewer_view import reviewer_row
from pipeline.schema import AppFinding, AppSeed, Evidence
from scripts import trace_research
from scripts.trace_research import execution_failure_category
import scripts.run_quality_research as quality_runner


SEED = AppSeed(id=1, name="Fixture Product", category="Test", hint_url="https://developer.example.com/docs")
URL = "https://www.example.com/product/overview"
DESCRIPTION = "Fixture Product is a customer platform for managing support requests, team workflows, customer data, and business communications."


def evidence(field: str, value: str | None = None, url: str = URL) -> dict[str, str]:
    item = {"field": field, "url": url}
    if value is not None:
        item["value"] = value
    return item


def payload(**updates: object) -> dict:
    base: dict = {
        "one_liner": DESCRIPTION,
        "auth_methods": ["oauth2"],
        "self_serve_status": "self_serve_free",
        "api_surface_types": ["rest"],
        "api_breadth_notes": "Tickets, users, files, events, and workflow actions.",
        "api_surface_breadth": "broad",
        "api_surface_summary": "Tickets, users, files, events, and workflow actions.",
        "mcp_status": "official",
        "mcp_exists": True,
        "buildability_verdict": "yes",
        "evidence": [
            evidence("one_liner"), evidence("auth_methods", "oauth2"), evidence("self_serve_status"),
            evidence("api_surface_types", "rest"), evidence("api_breadth_notes"), evidence("api_surface_breadth"),
            evidence("api_surface_summary"), evidence("mcp_status"), evidence("mcp_exists"), evidence("buildability_verdict"),
        ],
    }
    base.update(updates)
    return base


def normalize(data: dict) -> AppFinding:
    return normalize_payload(
        SEED, data, {URL}, [], False, {URL},
        {URL: "OAuth API key JWT REST GraphQL Webhooks credentials free sign up Model Context Protocol tickets users files events workflows."},
    )


@pytest.mark.parametrize(
    ("methods", "expected"),
    [
        (["oauth2"], ["oauth2"]),
        (["oauth2", "api_key"], ["oauth2", "api_key"]),
        (["oauth2", "api_key", "jwt"], ["oauth2", "api_key", "jwt"]),
        (["api_key", "basic_auth"], ["api_key", "basic"]),
    ],
)
def test_multi_auth_is_normalized_with_per_value_evidence(methods: list[str], expected: list[str]) -> None:
    finding = normalize(payload(auth_methods=methods, evidence=[evidence("one_liner"), *[evidence("auth_methods", method) for method in methods]]))
    assert finding.auth_methods == expected
    assert [item.value for item in finding.evidence if item.field == "auth_methods"] == expected


def test_provider_display_casing_is_normalized_before_evidence_validation() -> None:
    source_text = "The REST API uses an API Key for authentication."
    finding = normalize_payload(
        SEED,
        {
            "auth_methods": ["API Key"],
            "api_surface_types": ["REST"],
            "evidence": [
                evidence("auth_methods", "API Key"),
                evidence("api_surface_types", "REST"),
            ],
        },
        {URL}, [], False, {URL}, {URL: source_text},
    )
    assert finding.auth_methods == ["api_key"]
    assert finding.api_surface_types == ["rest"]


def test_field_suitable_source_recovers_missing_model_source_ids_without_guessing() -> None:
    source_text = (
        "Fixture Product is a customer platform for managing support requests and business workflows. "
        "Its REST API uses an API Key and exposes tickets, users, files, events, and workflow actions."
    )
    finding = normalize_payload(
        SEED,
        {
            "one_liner": DESCRIPTION,
            "auth_methods": ["API Key"],
            "api_surface_types": ["REST"],
            "api_breadth_notes": "Tickets, users, files, events, and workflow actions.",
            "evidence": [],
        },
        {URL}, [], False, set(), {URL: source_text},
    )
    assert finding.one_liner == DESCRIPTION
    assert finding.auth_methods == ["api_key"]
    assert finding.api_surface_types == ["rest"]
    assert finding.api_breadth_notes is not None
    assert {item.field for item in finding.evidence} >= {"one_liner", "auth_methods", "api_surface_types", "api_breadth_notes"}


def test_source_auth_header_and_api_headings_recover_without_model_values() -> None:
    source_text = """# API Reference
## Contacts
## Tags
## Campaigns
Authorization: Bearer API_KEY
"""
    finding = normalize_payload(
        SEED, {"auth_methods": [], "api_surface_types": [], "api_breadth_notes": None, "evidence": []},
        {URL}, [], False, set(), {URL: source_text},
    )
    assert {"api_key", "bearer_token"} <= set(finding.auth_methods)
    assert finding.api_breadth_notes is not None
    assert any(item.field == "api_breadth_notes" for item in finding.evidence)


def test_unknown_breadth_sentinel_does_not_block_source_recovery() -> None:
    source_text = """# API reference
## Tickets
## Users
## Workflow actions
"""
    finding = normalize_payload(
        SEED, {"api_breadth_notes": "Unknown", "evidence": []},
        {URL}, [], False, set(), {URL: source_text},
    )
    assert finding.api_breadth_notes is not None
    assert "Documented API reference covers" in finding.api_breadth_notes
    assert any(item.field == "api_breadth_notes" for item in finding.evidence)


@pytest.mark.parametrize(
    ("methods", "expected"),
    [
        ([{"type": "oauth2"}], []),
        ([None], []),
        (["oauth2", {"method": "api_key"}], ["oauth2"]),
        ("oauth2", []),
    ],
)
def test_malformed_auth_items_are_safe_and_keep_only_evidenced_strings(methods: object, expected: list[str]) -> None:
    finding = normalize(payload(auth_methods=methods, evidence=[evidence("one_liner"), evidence("auth_methods", "oauth2")]))
    assert finding.auth_methods == expected


@pytest.mark.parametrize(
    "surfaces",
    [
        ["rest"], ["rest", "graphql"], ["rest", "graphql", "webhooks"], ["rest", "soap", "sdk"],
    ],
)
def test_multi_api_surfaces_keep_all_evidenced_values(surfaces: list[str]) -> None:
    finding = normalize(payload(api_surface_types=surfaces, evidence=[evidence("one_liner"), *[evidence("api_surface_types", value) for value in surfaces]]))
    assert finding.api_surface_types == surfaces
    assert finding.api_surface_breadth == "unknown"  # breadth is independent evidence.


def test_unrelated_evidence_cannot_ground_auth_or_api() -> None:
    finding = normalize(payload(auth_methods=["oauth2"], api_surface_types=["rest"], evidence=[evidence("one_liner")]))
    assert finding.auth_methods == []
    assert finding.api_surface_types == []


class FakeSearchClient:
    async def search_web(self, query: str) -> str:
        if "Model Context Protocol" in query:
            return "https://www.example.com/ https://github.com/community/example-mcp"
        if "credentials account pricing" in query:
            return "https://www.example.com/pricing https://developer.example.com/docs/credentials"
        if "resources endpoints" in query or "REST GraphQL SOAP" in query:
            return "https://developer.example.com/oauth/authorize https://developer.example.com/docs/api/reference"
        if "authentication" in query or "API key bearer" in query:
            return "https://www.example.com/pricing https://developer.example.com/docs/authentication"
        return "https://www.example.com/product/overview https://developer.example.com/docs/api"


def test_source_discovery_keeps_field_scoped_candidates_without_misusing_oauth() -> None:
    urls, blockers = asyncio.run(discover_sources(SEED, FakeSearchClient()))
    assert not blockers
    assert "https://www.example.com/product/overview" in urls
    assert "https://developer.example.com/docs/authentication" in urls
    assert "https://developer.example.com/docs/credentials" in urls
    assert "https://developer.example.com/docs/api/reference" in urls
    assert "https://github.com/community/example-mcp" in urls
    assert "https://developer.example.com/oauth/authorize" not in urls


def test_access_discovery_retains_broad_first_party_commercial_and_eligibility_pages() -> None:
    class BroadAccessClient:
        async def search_web(self, query: str) -> str:
            return " ".join([
                "https://developer.example.com/docs/api",
                "https://www.example.com/pricing",
                "https://www.example.com/terms/api-fees",
                "https://support.example.com/api-account",
                "https://developer.example.com/onboarding/verification",
                "https://developer.example.com/app-review",
                "https://www.example.com/help/developer-access",
                "https://developer.example.com/docs/credentials",
            ])

    urls, blockers = asyncio.run(discover_sources(SEED, BroadAccessClient(), ("self_serve_status",)))
    assert not blockers
    assert "https://www.example.com/pricing" in urls
    assert "https://www.example.com/terms/api-fees" in urls
    assert "https://support.example.com/api-account" in urls
    assert "https://developer.example.com/onboarding/verification" in urls
    assert "https://developer.example.com/app-review" in urls


def test_description_discovery_accepts_only_a_generic_exact_brand_domain_alias() -> None:
    class AliasSearchClient:
        async def search_web(self, query: str) -> str:
            if "product platform overview" in query or "what is" in query:
                return "https://fixtureproduct.com/product/overview"
            return ""

    urls, _ = asyncio.run(discover_sources(SEED, AliasSearchClient(), ("one_liner",)))
    assert "https://fixtureproduct.com/product/overview" in urls


def test_description_discovery_accepts_a_generic_brand_prefixed_domain_alias() -> None:
    class AliasSearchClient:
        async def search_web(self, query: str) -> str:
            return "https://usefixtureproduct.com/product/overview"

    urls, blockers = asyncio.run(discover_sources(SEED, AliasSearchClient(), ("one_liner",)))
    assert not blockers
    assert "https://usefixtureproduct.com/product/overview" in urls


def test_field_discovery_accepts_a_generic_brand_domain_alias_but_keeps_field_scope() -> None:
    class AliasSearchClient:
        async def search_web(self, query: str) -> str:
            return "https://fixtureproduct.com/developers/api/authentication https://fixtureproduct.com/pricing"

    urls, blockers = asyncio.run(discover_sources(SEED, AliasSearchClient(), ("auth_methods",)))
    assert not blockers
    assert "https://fixtureproduct.com/developers/api/authentication" in urls
    assert "https://fixtureproduct.com/pricing" not in urls


def test_brand_domain_alias_ignores_a_parenthetical_seed_qualifier() -> None:
    qualified_seed = AppSeed(id=2, name="Fixture Product (Owner)", category="Test", hint_url="https://developer.owner.example/docs")

    class AliasSearchClient:
        async def search_web(self, query: str) -> str:
            return "https://fixtureproduct.com/product/overview"

    urls, blockers = asyncio.run(discover_sources(qualified_seed, AliasSearchClient(), ("one_liner",)))
    assert not blockers
    assert "https://fixtureproduct.com/product/overview" in urls


def test_description_discovery_keeps_multiple_product_pages_and_allows_an_official_overview() -> None:
    class ProductSearchClient:
        async def search_web(self, query: str) -> str:
            return " ".join([
                "https://www.example.com/product/overview",
                "https://www.example.com/features",
                "https://www.example.com/solutions",
                "https://www.example.com/about",
                "https://www.example.com/docs/product-overview",
                "https://www.example.com/docs/api/reference",
            ])

    urls, blockers = asyncio.run(discover_sources(SEED, ProductSearchClient(), ("one_liner",)))
    assert not blockers
    assert "https://www.example.com/product/overview" in urls
    assert "https://www.example.com/features" in urls
    assert "https://www.example.com/docs/product-overview" in urls
    assert "https://www.example.com/docs/api/reference" not in urls


def test_product_description_validation_accepts_a_plain_customer_purpose_sentence() -> None:
    description = "Fixture Product helps teams create, organize, and share documents, tasks, and project knowledge in one workspace."
    assert research_agent._is_product_description(description)


def test_prompt_compacts_sources_and_keeps_exact_source_urls() -> None:
    long_text = "Product purpose " + ("REST API tickets users workflows " * 1_000)
    prompt = _prompt(SEED, [(URL, long_text)], None)
    assert URL in prompt
    assert len(_relevant_excerpt(long_text)) <= 1_000


def test_focused_recovery_prompt_contains_only_its_requested_schema_and_rules() -> None:
    prompt = _prompt(SEED, [(URL, "Pricing includes a free trial. API objects include tickets and users.")], None, ("self_serve_status",))
    assert '"self_serve_status":"unknown"' in prompt
    assert '"api_surface_types"' not in prompt
    assert "Do not treat OAuth/API-key authentication as self-serve access" in prompt


def test_buildability_and_caveat_recovery_uses_one_combined_schema() -> None:
    prompt = _prompt(SEED, [(URL, "API documentation")], None, ("buildability_verdict", "blocker"))
    assert prompt.count('"buildability_verdict":null') == 1
    assert prompt.count('"blocker":null') == 1
    assert "never an HTTP" in prompt


def test_access_source_selection_rejects_oauth_endpoints() -> None:
    assert research_agent._field_suitable("https://developer.example.com/oauth/token", "self_serve_status") is False
    assert research_agent._field_suitable("https://www.example.com/pricing", "self_serve_status") is True


@pytest.mark.parametrize("url", [
    "https://www.example.com/favicon.ico",
    "https://images.example.com/logo.webp",
    "https://your.example.com/api/v3/tickets",
])
def test_source_selection_rejects_assets_and_template_subdomains(url: str) -> None:
    assert research_agent._field_suitable(url, "one_liner") is False


def test_fetch_discards_embedded_placeholder_links_from_a_requested_page() -> None:
    requested = "https://developer.example.com/docs/api"

    class EmbeddedLinkClient:
        async def fetch_urls_content(self, urls: list[str]):
            return [
                ("https://YOURDOMAIN.example.com/docs/api", "placeholder"),
                ("https://images.example.com/logo.webp", "asset"),
            ], None

    pages, blockers = asyncio.run(fetch_sources([requested], EmbeddedLinkClient()))
    assert pages == []
    assert blockers == [f"fetch returned no usable page text for {requested}"]


def test_source_only_trace_exercises_mcp_boundaries_without_calling_the_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    source_url = "https://www.example.com/product/overview"

    class SourceOnlyClient:
        async def capabilities(self):
            return type("Capabilities", (), {"fetch_tool": "fetch", "browser_tools": ["search"]})()

        async def search_web(self, query: str) -> str:
            return source_url

        async def fetch_urls_content(self, urls: list[str]):
            return [(source_url, "Fixture Product helps teams manage customer service and business workflows.")], None

    async def unexpected_llm(*args, **kwargs):
        raise AssertionError("source-only trace must not call the LLM")

    monkeypatch.setattr(trace_research, "list_seeds", lambda _: [SEED])
    monkeypatch.setattr(trace_research, "_extract_payload", unexpected_llm)
    result = asyncio.run(trace_research.trace(SEED.name, client=SourceOnlyClient(), source_only=True))
    assert result["preflight"] == {"status": "success", "llm_called": False, "fetched_page_count": 1}


def test_generic_official_free_access_source_recovers_when_model_omits_access() -> None:
    access_url = "https://developer.example.com/docs/getting-started"
    finding = normalize_payload(
        SEED, {"self_serve_status": "unknown", "evidence": []}, {access_url}, [], False,
        set(), {access_url: "The developer API is free to use. Create an application to obtain credentials."},
    )
    assert finding.self_serve_status == "self_serve_free"
    assert any(item.field == "self_serve_status" and item.value == "self_serve_free" for item in finding.evidence)


@pytest.mark.parametrize("official_language", [
    "The API is available free of charge to developers.",
    "Developer access is available at no cost to all developers.",
    "This API is open to all developers without charge.",
])
def test_generic_access_classifier_recognizes_explicit_free_access_variants(official_language: str) -> None:
    access_url = "https://developer.example.com/docs/access"
    finding = normalize_payload(
        SEED, {"self_serve_status": "unknown", "evidence": []}, {access_url}, [], False,
        set(), {access_url: official_language},
    )
    assert finding.self_serve_status == "self_serve_free"


def test_generic_access_classifier_does_not_treat_auth_words_as_credential_availability() -> None:
    access_url = "https://developer.example.com/docs/oauth"
    finding = normalize_payload(
        SEED, {"self_serve_status": "unknown", "evidence": []}, {access_url}, [], False,
        set(), {access_url: "Use OAuth tokens to authenticate API requests."},
    )
    assert finding.self_serve_status == "unknown"


def test_generic_access_classifier_recognizes_self_service_account_setup() -> None:
    access_url = "https://developer.example.com/docs/getting-started"
    finding = normalize_payload(
        SEED, {"self_serve_status": "unknown", "evidence": []}, {access_url}, [], False,
        set(), {access_url: "Create an account and register an application to obtain API credentials."},
    )
    assert finding.self_serve_status == "self_serve_account_required"


def test_generic_access_classifier_recognizes_admin_enabled_api_access() -> None:
    access_url = "https://developer.example.com/docs/api-keys"
    finding = normalize_payload(
        SEED, {"self_serve_status": "unknown", "evidence": []}, {access_url}, [], False,
        set(), {access_url: "An administrator must enable API access before developers can create an API key."},
    )
    assert finding.self_serve_status == "admin_approval_required"


def test_access_evidence_alias_is_canonicalized_without_losing_provenance() -> None:
    access_url = "https://developer.example.com/docs/access"
    finding = normalize_payload(
        SEED,
        {"self_serve_status": "self_serve_account_required", "evidence": [
            {"field": "self_serve_access", "value": "self_serve_account_required", "url": access_url},
        ]},
        {access_url}, [], False, set(), {access_url: "Create an account to obtain API credentials."},
    )
    assert finding.self_serve_status == "self_serve_account_required"
    assert finding.evidence[0].field == "self_serve_status"


def test_free_offering_and_self_service_setup_require_two_generic_official_sources() -> None:
    free_url = "https://www.example.com/"
    setup_url = "https://developer.example.com/docs/apps"
    finding = normalize_payload(
        SEED, {"self_serve_status": "unknown", "evidence": []}, {free_url, setup_url}, [], False,
        set(), {
            free_url: "Our service is available free of charge.",
            setup_url: "Developers can create an application to obtain API credentials.",
        },
    )
    assert finding.self_serve_status == "self_serve_free"
    assert {item.url.unicode_string() for item in finding.evidence if item.field == "self_serve_status"} == {free_url, setup_url}


def test_usage_pricing_and_automatic_access_prove_generic_self_serve_paid() -> None:
    price_url = "https://www.example.com/pricing/api"
    access_url = "https://developer.example.com/docs/app-review"
    finding = normalize_payload(
        SEED, {"self_serve_status": "unknown", "evidence": []}, {price_url, access_url}, [], False,
        set(), {
            price_url: "The API is charged on a per-message, usage-based basis.",
            access_url: "Business applications are automatically approved for Standard Access.",
        },
    )
    assert finding.self_serve_status == "self_serve_paid"
    assert {item.url.unicode_string() for item in finding.evidence if item.field == "self_serve_status"} == {price_url, access_url}


def test_source_derived_access_recovery_preserves_existing_fields_without_an_llm_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Focused Access recovery can complete directly from explicit official text."""
    access_url = "https://developer.example.com/docs/getting-started"
    existing = AppFinding(
        app_id=SEED.id, category=SEED.category, one_liner=DESCRIPTION,
        auth_methods=["oauth2"], self_serve_status="unknown",
        api_surface_type="rest", api_surface_types=["rest"],
        api_breadth_notes="Tickets and users.", buildability_verdict="yes_with_caveats",
        confidence=.6, research_status="partial",
        evidence=[
            Evidence(field="one_liner", url=URL),
            Evidence(field="auth_methods", value="oauth2", url=URL),
            Evidence(field="api_surface_types", value="rest", url=URL),
            Evidence(field="api_breadth_notes", url=URL),
        ],
    )

    class SourceOnlyClient:
        async def search_web(self, query: str) -> str:
            return access_url

        async def fetch_urls_content(self, urls: list[str]):
            return [(access_url, "The developer API is free to use. Create an application to obtain credentials.")], None

    async def unexpected_llm(*args, **kwargs):
        raise AssertionError("explicit official Access evidence must avoid an LLM call")

    monkeypatch.setattr(quality_recovery, "_extract_payload", unexpected_llm)
    result = asyncio.run(quality_recovery.recover_quality(
        SEED, existing, SourceOnlyClient(), ("self_serve_status",),
    ))
    recovered = AppFinding.model_validate(result["finding"])
    assert result["status"] == "source_derived"
    assert recovered.self_serve_status == "self_serve_free"
    assert recovered.one_liner == existing.one_liner
    assert recovered.auth_methods == existing.auth_methods
    assert recovered.api_surface_types == existing.api_surface_types


def test_source_only_recovery_never_calls_llm_when_access_remains_unresolved(monkeypatch: pytest.MonkeyPatch) -> None:
    existing = AppFinding(app_id=SEED.id, category=SEED.category, self_serve_status="unknown", confidence=0.0, research_status="partial")

    class NoAccessClient:
        async def search_web(self, query: str) -> str:
            return "https://developer.example.com/docs/oauth"

        async def fetch_urls_content(self, urls: list[str]):
            return [(urls[0], "Use OAuth tokens to authenticate API requests.")], None

    async def unexpected_llm(*args, **kwargs):
        raise AssertionError("source-only recovery must not invoke the LLM")

    monkeypatch.setattr(quality_recovery, "_extract_payload", unexpected_llm)
    result = asyncio.run(quality_recovery.recover_quality(
        SEED, existing, NoAccessClient(), ("self_serve_status",), allow_llm=False,
    ))
    assert result["status"] == "source_only_pending"
    assert AppFinding.model_validate(result["finding"]).self_serve_status == "unknown"


def test_prompt_uses_short_source_ids_and_expands_them_before_validation() -> None:
    prompt = _prompt(SEED, [(URL, "Customer platform documentation")], None)
    assert "S1" in prompt
    assert URL in prompt
    assert "yes_with_caveats" in prompt
    expanded = _expand_source_ids({"evidence": [{"field": "one_liner", "url": "S1"}]}, [(URL, "text")])
    assert expanded["evidence"][0]["url"] == URL


@pytest.mark.parametrize("source_id", ["1", "[1]", "S1", "[S1]"])
def test_source_id_recovery_accepts_numeric_and_s_prefixed_model_references(source_id: str) -> None:
    expanded = _expand_source_ids({"evidence": [{"field": "one_liner", "url": source_id}]}, [(URL, "text")])
    assert expanded["evidence"][0]["url"] == URL


def test_compact_extraction_context_uses_at_most_one_page_per_dimension() -> None:
    sources = [
        ("https://example.com/product/overview", "product"),
        ("https://example.com/docs/authentication", "auth"),
        ("https://example.com/docs/credentials", "access"),
        ("https://example.com/docs/api/reference", "api"),
        ("https://example.com/docs/resources", "breadth"),
        ("https://example.com/docs/mcp", "mcp"),
        ("https://example.com/docs/other", "unused"),
    ]
    compact = _compact_sources_for_extraction(sources)
    assert len(compact) <= 6
    assert {url for url, _ in compact} >= {"https://example.com/product/overview", "https://example.com/docs/authentication", "https://example.com/docs/mcp"}


def test_each_extraction_attempt_makes_one_llm_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def fake_call(prompt: str, budget: int):
        nonlocal calls
        calls += 1
        assert budget == 700
        return {"evidence": []}

    monkeypatch.setenv("LLM_INITIAL_MAX_TOKENS", "700")
    monkeypatch.setattr(research_agent, "_call_llm", fake_call)
    assert asyncio.run(_extract_payload(SEED, [(URL, "source")], None)) == {"evidence": []}
    assert calls == 1


class FakeFetchClient:
    async def fetch_urls_content(self, urls: list[str]):
        if "bad" in urls[0]:
            raise RuntimeError("simulated fetch failure")
        return [(urls[0], "official source text")], None


def test_fetch_failure_isolated_and_keeps_other_field_sources() -> None:
    sources, blockers = asyncio.run(fetch_sources(["https://example.com/good", "https://example.com/bad"], FakeFetchClient()))
    assert sources == [("https://example.com/good", "official source text")]
    assert blockers == ["fetch failed for https://example.com/bad: RuntimeError"]


def test_json_parser_handles_wrapped_json_and_rejects_malformed_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(research_agent, "_call_llm_raw", lambda *args, **kwargs: "```json\n{\"ok\": true}\n```")
    assert research_agent._call_llm("fixture") == {"ok": True}
    monkeypatch.setattr(research_agent, "_call_llm_raw", lambda *args, **kwargs: "not JSON")
    with pytest.raises(Exception):
        research_agent._call_llm("fixture")
    assert _decode_json_object('provider preface {"ok": true} trailing {not-json') == {"ok": True}
    with pytest.raises(LLMResponseFormatError):
        _decode_json_object("provider prose without a JSON object")


def test_mcp_requires_explicit_mcp_source_text() -> None:
    finding = normalize_payload(
        SEED,
        {"mcp_status": "official", "mcp_exists": True, "evidence": [evidence("mcp_status"), evidence("mcp_exists")]},
        {URL}, [], False, set(), {URL: "Generic product homepage without the required protocol mention."},
    )
    assert finding.mcp_status == "unknown"
    assert finding.mcp_exists is None


def test_mcp_schema_shape_recovery_preserves_an_evidenced_legacy_classification() -> None:
    finding = normalize_payload(
        SEED,
        {"mcp_status": "unknown", "mcp_exists": "official", "evidence": [evidence("mcp_exists", "official")]},
        {URL}, [], False, {URL}, {URL: "This documentation introduces the official Model Context Protocol MCP server."},
    )
    assert finding.mcp_status == "official"
    assert finding.mcp_exists is True


def test_generic_first_party_mcp_source_recovers_when_model_returns_unknown() -> None:
    mcp_url = "https://developer.example.com/docs/mcp-server"
    finding = normalize_payload(
        SEED,
        {"mcp_status": "unknown", "evidence": []},
        {mcp_url}, [], False, {mcp_url},
        {mcp_url: "This guide introduces our Model Context Protocol MCP server for integrations."},
    )
    assert finding.mcp_status == "official"
    assert finding.mcp_exists is True
    assert finding.mcp_notes == "First-party documentation explicitly presents an MCP server."
    assert any(item.field == "mcp_status" and item.url.unicode_string() == mcp_url for item in finding.evidence)


def test_generic_community_mcp_repository_recovers_when_model_returns_unknown() -> None:
    community_url = "https://github.com/example-org/example-mcp-server"
    finding = normalize_payload(
        SEED,
        {"mcp_status": "unknown", "evidence": []},
        {community_url}, [], False, {community_url},
        {community_url: "An MCP server implementation for the Example API."},
    )
    assert finding.mcp_status == "community"
    assert finding.mcp_exists is True
    assert finding.evidence[-1].source_type == "community_repository"


def test_buildability_is_derived_from_evidenced_api_and_auth_without_model_guessing() -> None:
    finding = normalize(payload(
        one_liner=None,
        self_serve_status="unknown",
        api_surface_types=["rest"],
        auth_methods=["oauth2"],
        buildability_verdict=None,
        evidence=[evidence("auth_methods", "oauth2"), evidence("api_surface_types", "rest")],
    ))
    assert finding.buildability_verdict == "yes_with_caveats"
    assert finding.blocker == "Credential access unresolved"
    assert any(item.field == "buildability_verdict" for item in finding.evidence)


def test_developer_oauth_page_cannot_be_description_evidence() -> None:
    oauth_url = "https://developer.example.com/docs/oauth"
    finding = normalize_payload(
        SEED,
        {"one_liner": DESCRIPTION, "evidence": [evidence("one_liner", url=oauth_url)]},
        {oauth_url}, [], False, set(), {oauth_url: "OAuth documentation"},
    )
    assert finding.one_liner is None


def test_focused_recovery_is_field_level_and_preserves_grounded_values() -> None:
    initial = AppFinding(
        app_id=1, category="Test", one_liner=DESCRIPTION, auth_methods=["oauth2"], self_serve_status="unknown",
        api_surface_type="rest", api_surface_types=["rest"], api_breadth_notes="Tickets and users.",
        mcp_status="unknown", buildability_verdict="yes", confidence=.7, research_status="partial",
        evidence=[Evidence(field="one_liner", url=URL), Evidence(field="auth_methods", value="oauth2", url=URL), Evidence(field="api_surface_types", value="rest", url=URL), Evidence(field="api_breadth_notes", url=URL), Evidence(field="buildability_verdict", url=URL)],
    )
    assert targets_for(initial) == ("self_serve_status", "mcp_status", "buildability_verdict", "blocker")
    recovered = AppFinding(
        app_id=1, category="Test", self_serve_status="self_serve_free", mcp_status="community", mcp_exists=True,
        confidence=.3, research_status="partial", evidence=[Evidence(field="self_serve_status", url=URL), Evidence(field="mcp_status", url=URL)],
    )
    merged = merge_quality_recovery(initial, recovered, targets_for(initial))
    assert merged.one_liner == DESCRIPTION
    assert merged.auth_methods == ["oauth2"]
    assert merged.self_serve_status == "self_serve_free"
    assert merged.mcp_status == "community"


def test_stale_generic_caveat_is_a_generic_recovery_target_after_access_is_resolved() -> None:
    finding = AppFinding(
        app_id=SEED.id, category=SEED.category, self_serve_status="self_serve_free",
        buildability_verdict="yes_with_caveats", blocker="Credential access unresolved",
        confidence=.5, research_status="partial",
    )
    assert {"buildability_verdict", "blocker"} <= set(targets_for(finding))


def test_projection_keeps_multi_values_and_never_shows_execution_failure_as_product_blocker() -> None:
    finding = normalize(payload(auth_methods=["oauth2", "api_key"], api_surface_types=["rest", "graphql"], evidence=[
        evidence("one_liner"), evidence("auth_methods", "oauth2"), evidence("auth_methods", "api_key"),
        {"field": "self_serve_status", "url": URL, "note": "Developer credential access is available after sign up."}, evidence("api_surface_types", "rest"), evidence("api_surface_types", "graphql"),
        evidence("api_breadth_notes"), evidence("api_surface_breadth"), evidence("api_surface_summary"),
        evidence("mcp_status"), evidence("mcp_exists"), evidence("buildability_verdict"),
    ]))
    row = reviewer_row("Fixture Product", finding)
    assert row.auth == "OAuth2 · API Key"
    assert row.api_surface.startswith("REST + GraphQL")
    assert row.what_it_does == DESCRIPTION
    assert {item["label"] for item in row.evidence} >= {"Description", "Auth", "Access", "API", "MCP"}
    failed = AppFinding(app_id=1, category="Test", confidence=0, research_status="unresolved", research_blockers=["research_execution_failure:provider_402"])
    failed_row = reviewer_row("Fixture Product", failed)
    assert failed_row.buildability == "Unknown"
    assert "402" not in failed_row.main_blocker_or_caveat


def test_provider_failure_classification_is_not_a_genuine_unknown() -> None:
    assert execution_failure_category(LLMRequestError(402, "billing")) == "provider_402"
    assert execution_failure_category(LLMRequestError(429, "rate limited")) == "provider_429"
    assert execution_failure_category(LLMResponseFormatError("bad JSON")) == "parser_failure"
    assert execution_failure_category(LLMRequestError(200, "aicredits returned no final content")) == "provider_empty_content"


def test_execution_failure_does_not_repeat_a_full_canary_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def failed_trace(*args, **kwargs):
        nonlocal calls
        calls += 1
        return {"validation": {"execution_failure": "provider_429", "finding": AppFinding(app_id=1, category="Test", confidence=0, research_status="unresolved", research_blockers=["research_execution_failure:provider_429"]).model_dump(mode="json")}}

    monkeypatch.setattr(quality_runner, "ComposioResearchMCP", lambda: object())
    monkeypatch.setattr(quality_runner, "trace", failed_trace)
    result = asyncio.run(quality_runner.research_with_quality(SEED, max_llm_calls=4))
    assert calls == 1
    assert result["validation"]["execution_failure"] == "provider_429"


def test_runner_performs_at_most_one_targeted_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    trace_calls = 0
    recovery_calls = 0
    initial = AppFinding(
        app_id=1, category="Test", one_liner=DESCRIPTION, auth_methods=["oauth2"],
        api_surface_type="rest", api_surface_types=["rest"], api_breadth_notes="Tickets and users.",
        confidence=.5, research_status="partial",
        evidence=[Evidence(field="one_liner", url=URL), Evidence(field="auth_methods", value="oauth2", url=URL), Evidence(field="api_surface_types", value="rest", url=URL), Evidence(field="api_breadth_notes", url=URL)],
    )

    async def initial_trace(*args, **kwargs):
        nonlocal trace_calls
        trace_calls += 1
        return {"validation": {"finding": initial.model_dump(mode="json")}}

    async def one_recovery(*args, **kwargs):
        nonlocal recovery_calls
        recovery_calls += 1
        return {"finding": initial.model_dump(mode="json")}

    monkeypatch.setattr(quality_runner, "ComposioResearchMCP", lambda: object())
    monkeypatch.setattr(quality_runner, "trace", initial_trace)
    monkeypatch.setattr(quality_runner, "recover_quality", one_recovery)
    asyncio.run(quality_runner.research_with_quality(SEED, max_llm_calls=2))
    assert trace_calls == 1
    assert recovery_calls == 1


def test_runner_uses_one_combined_targeted_recovery_under_the_cost_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    initial = AppFinding(
        app_id=1, category="Test", one_liner=DESCRIPTION, confidence=.2, research_status="partial",
        evidence=[Evidence(field="one_liner", url=URL)],
    )
    recovery_calls = 0

    async def initial_trace(*args, **kwargs):
        return {"llm": {"fallback_used": False}, "validation": {"finding": initial.model_dump(mode="json")}}

    async def unresolved_recovery(*args, **kwargs):
        nonlocal recovery_calls
        recovery_calls += 1
        return {"targets": ["auth_methods"], "finding": initial.model_dump(mode="json")}

    monkeypatch.setattr(quality_runner, "ComposioResearchMCP", lambda: object())
    monkeypatch.setattr(quality_runner, "trace", initial_trace)
    monkeypatch.setattr(quality_runner, "recover_quality", unresolved_recovery)
    result = asyncio.run(quality_runner.research_with_quality(SEED, max_llm_calls=4))
    assert recovery_calls == 1
    assert [item["attempt"] for item in result["quality_recovery_attempts"]] == [2]


def test_json_mode_empty_response_fallback_never_creates_a_third_call(monkeypatch: pytest.MonkeyPatch) -> None:
    initial = AppFinding(app_id=1, category="Test", confidence=0, research_status="unresolved", research_blockers=["no grounded evidence for one_liner"])

    async def fallback_trace(*args, **kwargs):
        return {"llm": {"fallback_used": True}, "validation": {"finding": initial.model_dump(mode="json")}}

    async def should_not_recover(*args, **kwargs):
        raise AssertionError("empty-response fallback already consumed the second LLM call")

    monkeypatch.setattr(quality_runner, "ComposioResearchMCP", lambda: object())
    monkeypatch.setattr(quality_runner, "trace", fallback_trace)
    monkeypatch.setattr(quality_runner, "recover_quality", should_not_recover)
    result = asyncio.run(quality_runner.research_with_quality(SEED, max_llm_calls=2))
    assert result["llm"]["fallback_used"] is True


def test_aicredits_uses_configured_openai_compatible_endpoint_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"model":"z-ai/glm-5.2","usage":{"prompt_tokens":111,"completion_tokens":22,"completion_tokens_details":{"reasoning_tokens":3},"cost":0.01,"credits":0.05},"choices":[{"message":{"content":[{"type":"text","text":"{\\"ok\\":true}"}]},"finish_reason":"stop"}]}'

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode())
        return Response()

    monkeypatch.setenv("LLM_PROVIDER", "aicredits")
    monkeypatch.setenv("AICREDITS_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "z-ai/glm-5.2")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.aicredits.in/v1")
    monkeypatch.setattr(research_agent, "urlopen", fake_urlopen)
    assert research_agent._call_llm("fixture") == {"ok": True}
    assert captured["url"] == "https://api.aicredits.in/v1/chat/completions"
    assert captured["body"]["model"] == "z-ai/glm-5.2"
    assert "max_tokens" in captured["body"]
    assert "max_completion_tokens" not in captured["body"]
    _, usage = research_agent._call_llm_with_usage("fixture")
    assert usage == {"input_tokens": 111, "output_tokens": 22, "reasoning_tokens": 3, "cost": 0.01, "credits": 0.05, "model": "z-ai/glm-5.2"}
    assert research_agent._call_llm("fixture", json_mode=False) == {"ok": True}
    assert "response_format" not in captured["body"]
