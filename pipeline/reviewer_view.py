"""Reviewer-facing projection of evidence-first findings.

This module is deliberately read-only: it translates an auditable AppFinding
into the assignment's table contract without mutating SQLite history or source
payloads.
"""
from __future__ import annotations

from dataclasses import dataclass

from pipeline.schema import AppFinding, Evidence


CATEGORY_LABELS = {
    "CRM and Sales": "CRM & Sales",
    "Support and Helpdesk": "Support & Helpdesk",
    "Communications and Messaging": "Communications & Messaging",
    "Marketing Ads Email and Social": "Marketing, Ads, Email & Social",
    "Data SEO and Scraping": "Data, SEO & Scraping",
    "Developer Infra and Data Platforms": "Developer, Infra & Data Platforms",
    "Productivity and Project Management": "Productivity & Project Management",
    "Finance and Fintech": "Finance & Fintech",
    "AI Research and Media-native": "AI, Research & Media-native",
}
AUTH_LABELS = {
    "oauth2": "OAuth2", "api_key": "API Key", "basic": "Basic Auth",
    "token": "Token", "bearer_token": "Bearer Token",
    "personal_access_token": "Personal Access Token", "bot_token": "Bot Token",
    "private_app_token": "Private App Token", "jwt": "JWT", "service_account": "Service Account",
    "signed_request": "Signed Request", "session_token": "Session Token", "client_credentials": "Client Credentials", "other": "Other",
}
ACCESS_LABELS = {
    "self_serve_free": "Self-serve free", "self_serve_trial": "Self-serve trial", "self_serve_paid": "Self-serve paid",
    "self_serve_account_required": "Self-serve account required",
    "paid_plan_required": "Paid plan required", "admin_approval_required": "Admin approval required",
    "partner_gated": "Partner approval required", "partner_approval_required": "Partner approval required",
    "contact_sales": "Contact sales", "enterprise_only": "Enterprise-only", "unknown": "Unknown",
}
API_LABELS = {
    "rest": "REST", "graphql": "GraphQL", "rest_and_graphql": "REST + GraphQL",
    "sdk_only": "SDK only", "other": "Other", "none_public": "No public API", "unknown": "Unknown",
}
API_TYPE_LABELS = {
    "rest": "REST", "graphql": "GraphQL", "soap": "SOAP", "grpc": "gRPC",
    "websocket": "WebSocket", "webhooks": "Webhooks", "sdk": "SDK", "rpc": "RPC", "other": "Other",
}


@dataclass(frozen=True)
class ReviewerRow:
    app: str
    category: str
    what_it_does: str
    auth: str
    access: str
    api_surface: str
    mcp: str
    buildability: str
    main_blocker_or_caveat: str
    evidence: list[dict[str, str]]
    grounded_fields: list[str]
    unresolved_fields: list[str]
    evidence_coverage: str
    research_status: str


def _evidence_for(finding: AppFinding, *fields: str) -> Evidence | None:
    for field in fields:
        for evidence in reversed(finding.evidence):
            if evidence.field == field:
                return evidence
    return None


def _access_is_credential_backed(finding: AppFinding) -> bool:
    # The normalizer has already checked the fetched source text for explicit
    # access-language before accepting this value.  Re-checking only the short
    # model note/URL here loses valid values in the reviewer projection.
    return _evidence_for(finding, "self_serve_status") is not None


def _evidenced_auth_methods(finding: AppFinding) -> list[str]:
    """Keep only auth values supported by field-level evidence text."""
    signals = {
        "oauth2": ("oauth",), "api_key": ("api key",), "basic": ("basic auth", "http basic"),
        "token": ("token",), "bearer_token": ("bearer",), "personal_access_token": ("personal access token",),
        "private_app_token": ("private app token",), "bot_token": ("bot token",), "jwt": ("jwt", "json web token"), "service_account": ("service account",),
        "signed_request": ("signed request", "signature"), "session_token": ("session token",), "client_credentials": ("client credentials",),
    }
    items = [item for item in finding.evidence if item.field == "auth_methods"]
    return [
        method for method in finding.auth_methods
        if any(
            item.value == method or (
                item.value is None
                and any(signal in f"{item.note or ''} {item.url}".lower() for signal in signals.get(method, ()))
            )
            for item in items
        )
    ]


def _evidenced_api_surface_types(finding: AppFinding) -> list[str]:
    """Project only API surfaces with evidence for the exact listed value."""
    current = list(finding.api_surface_types)
    items = [item for item in finding.evidence if item.field == "api_surface_types"]
    if current:
        return [
            value for value in current
            if any(
                item.value == value or (len(current) == 1 and item.value is None)
                for item in items
            )
        ]
    if finding.api_surface_type == "unknown":
        return []
    legacy = _evidence_for(finding, "api_surface_type")
    return [finding.api_surface_type] if legacy else []


def _is_reviewer_ready_description(value: str | None) -> bool:
    """Reject integration-only text from the product-purpose column.

    This is a projection safeguard, not a mutation of the append-only finding.
    A weak description remains auditable in its trace but cannot masquerade as a
    customer-facing product explanation in the reviewer table.
    """
    if not value:
        return False
    text = value.lower()
    integration_signals = ("offers rest api", "provides rest api", "api platform", "api reference", "hosted mcp")
    product_signals = ("platform", "crm", "manage", "communication", "messaging", "customer", "teams", "business")
    return not any(signal in text for signal in integration_signals) or any(signal in text for signal in product_signals)


def _breadth(finding: AppFinding) -> str:
    if finding.api_surface_breadth != "unknown":
        return finding.api_surface_breadth.capitalize()
    note = finding.api_breadth_notes or ""
    # A transparent, content-based legacy mapping lets historical fields appear
    # in the new contract without changing their stored values.
    if note.count(",") >= 3 or " and more" in note.lower() or "comprehensive" in note.lower() or "broad" in note.lower():
        return "Broad"
    return "Moderate" if note else "Unknown"


def _mcp_label(finding: AppFinding) -> str:
    notes = (finding.mcp_notes or "").lower()
    if "proof of concept" in notes or "proof-of-concept" in notes or "experimental" in notes or finding.mcp_status == "proof_of_concept":
        return "Proof-of-concept / experimental MCP"
    if finding.mcp_status == "official":
        return "Official MCP"
    if finding.mcp_status == "community":
        return "Community MCP"
    if finding.mcp_status == "no_evidence":
        return "None evidenced"
    return "Unknown"


def _status(finding: AppFinding, unresolved: list[str]) -> str:
    if finding.research_status in {"grounded", "complete"} and not unresolved:
        return "Grounded"
    if finding.research_status == "unresolved":
        return "Unresolved"
    return "Partial" + (": " + ", ".join(unresolved) + " unresolved" if unresolved else "")


def reviewer_row(name: str, finding: AppFinding) -> ReviewerRow:
    unresolved: list[str] = []
    description_is_grounded = _is_reviewer_ready_description(finding.one_liner) and _evidence_for(finding, "one_liner") is not None
    if not description_is_grounded: unresolved.append("Description")
    auth_methods = _evidenced_auth_methods(finding)
    if not auth_methods: unresolved.append("Auth")
    access_is_grounded = finding.self_serve_status != "unknown" and _access_is_credential_backed(finding)
    if not access_is_grounded: unresolved.append("Access")
    api_types = _evidenced_api_surface_types(finding)
    if not api_types: unresolved.append("API type")
    api_breadth_is_grounded = (
        bool(finding.api_breadth_notes or finding.api_surface_summary)
        and _evidence_for(finding, "api_surface_summary", "api_breadth_notes") is not None
    ) or (
        finding.api_surface_breadth != "unknown"
        and _evidence_for(finding, "api_surface_breadth") is not None
    )
    if not api_breadth_is_grounded: unresolved.append("API breadth")
    mcp_is_grounded = finding.mcp_status != "unknown" and _evidence_for(finding, "mcp_status", "mcp_exists") is not None
    if not mcp_is_grounded: unresolved.append("MCP")
    buildability_is_grounded = finding.buildability_verdict is not None and _evidence_for(finding, "buildability_verdict") is not None
    if not buildability_is_grounded: unresolved.append("Buildability")

    what_it_does = finding.one_liner if description_is_grounded else "Unknown"
    auth = " · ".join(AUTH_LABELS[item] for item in auth_methods) or "Unknown"
    access = ACCESS_LABELS[finding.self_serve_status] if access_is_grounded else "Unknown"
    api_type = " + ".join(API_TYPE_LABELS.get(item, API_LABELS.get(item, "Other")) for item in api_types) or "Unknown"
    api_summary = (finding.api_surface_summary or finding.api_breadth_notes) if api_breadth_is_grounded else None
    breadth_display = f"Breadth: {_breadth(finding)}"
    api_surface = f"{api_type}\n{breadth_display} — {api_summary}" if api_summary else f"{api_type}\n{breadth_display}"
    mcp = _mcp_label(finding) if mcp_is_grounded else "Unknown"
    if not buildability_is_grounded:
        buildability = "Unknown"
    elif finding.buildability_verdict == "yes":
        buildability = "Yes — with caveats" if not access_is_grounded or finding.self_serve_status in {"admin_approval_required", "partner_gated", "partner_approval_required", "contact_sales", "paid_plan_required"} else "Yes"
    elif finding.buildability_verdict == "yes_with_caveats":
        buildability = "Yes — with caveats"
    elif finding.buildability_verdict == "no":
        buildability = "No"
    else:
        buildability = "Unknown"
    blocker_is_grounded = finding.blocker and _evidence_for(finding, "blocker") is not None
    # A blank/unattempted trace has no product conclusion.  Do not recycle an
    # unresolved credential field into a misleading, identical "blocker" for
    # every app in the fixed 100-row live report.
    has_product_evidence = bool(finding.evidence)
    if finding.research_status == "unresolved" and not has_product_evidence:
        caveat = "Research pending — no product conclusion"
    elif finding.research_status == "unresolved":
        caveat = "Research incomplete — no product conclusion"
    elif blocker_is_grounded:
        caveat = finding.blocker
    elif not access_is_grounded and mcp == "Proof-of-concept / experimental MCP":
        documented_auth = " · ".join(AUTH_LABELS[item] for item in auth_methods)
        credential_caveat = (
            f"Credential access unresolved — documented authentication: {documented_auth}"
            if documented_auth else "Credential-access requirements unresolved"
        )
        caveat = credential_caveat + "; MCP is proof-of-concept / experimental"
    elif not access_is_grounded:
        documented_auth = " · ".join(AUTH_LABELS[item] for item in auth_methods)
        caveat = (
            f"Credential access unresolved — documented authentication: {documented_auth}"
            if documented_auth else "Credential-access requirements unresolved"
        )
    elif finding.self_serve_status == "admin_approval_required":
        caveat = "Admin approval required"
    elif mcp == "Proof-of-concept / experimental MCP":
        caveat = "MCP is proof-of-concept / experimental"
    else:
        caveat = "None material identified"

    evidence_specs = [
        ("Description", ("one_liner",)), ("Auth", ("auth_methods",)),
        ("Access", ("self_serve_status",)), ("API", ("api_surface_summary", "api_breadth_notes", "api_surface_types", "api_surface_type")),
        ("MCP", ("mcp_status", "mcp_exists")),
    ]
    evidence = []
    for label, fields in evidence_specs:
        item = _evidence_for(finding, *fields)
        if item and not (label == "Description" and not description_is_grounded) and not (label == "Access" and not access_is_grounded) and not (label == "API" and not (api_types or api_breadth_is_grounded)) and not (label == "MCP" and not mcp_is_grounded):
            evidence.append({"label": label, "url": str(item.url), "source_type": item.source_type})
    grounded = [field for field in ("Description", "Auth", "Access", "API type", "API breadth", "MCP", "Buildability") if field not in unresolved]
    return ReviewerRow(
        app=name, category=CATEGORY_LABELS.get(finding.category, finding.category), what_it_does=what_it_does,
        auth=auth, access=access, api_surface=api_surface, mcp=mcp, buildability=buildability,
        main_blocker_or_caveat=caveat, evidence=evidence, grounded_fields=grounded,
        unresolved_fields=unresolved, evidence_coverage=f"{len(grounded)}/7 reviewer fields evidenced",
        research_status=_status(finding, unresolved),
    )
