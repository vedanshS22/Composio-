from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

AuthMethod = Literal["oauth2", "api_key", "basic", "token", "bearer_token", "personal_access_token", "private_app_token", "bot_token", "jwt", "service_account", "signed_request", "session_token", "client_credentials", "other"]
AUTH_METHOD_ALIASES = {"basic_auth": "basic"}
SelfServeStatus = Literal["self_serve_free", "self_serve_trial", "self_serve_paid", "self_serve_account_required", "paid_plan_required", "admin_approval_required", "partner_gated", "partner_approval_required", "contact_sales", "enterprise_only", "unknown"]
ApiSurface = Literal["rest", "graphql", "rest_and_graphql", "sdk_only", "other", "none_public", "unknown"]
ApiSurfaceType = Literal["rest", "graphql", "soap", "grpc", "websocket", "webhooks", "sdk", "rpc", "other"]
ApiSurfaceBreadth = Literal["broad", "moderate", "narrow", "unknown"]
Verdict = Literal["yes", "yes_with_caveats", "no"]
# `complete` is retained only to read historical pass payloads. New findings
# use the reviewer-facing `grounded` status.
ResearchStatus = Literal["grounded", "complete", "partial", "unresolved"]
McpStatus = Literal["official", "community", "proof_of_concept", "no_evidence", "unknown"]
VerificationStatus = Literal["pending", "verified", "not_verified"]


class AppSeed(BaseModel):
    id: int
    name: str
    category: str
    hint_url: HttpUrl


class Evidence(BaseModel):
    field: str
    # For inherently multi-valued fields this identifies the exact item the
    # source supports (for example auth_methods/api_key or api_surface_types/graphql).
    value: str | None = None
    url: HttpUrl
    note: str | None = None
    source_title: str | None = None
    source_type: Literal["official_docs", "community_repository"] = "official_docs"
    confidence: float | None = Field(default=None, ge=0, le=1)
    verification_status: VerificationStatus = "pending"


class AppFinding(BaseModel):
    app_id: int
    category: str
    one_liner: str | None = None
    auth_methods: list[AuthMethod] = Field(default_factory=list)
    auth_other_label: str | None = None
    self_serve_status: SelfServeStatus = "unknown"
    gating_reason: str | None = None
    api_surface_type: ApiSurface = "unknown"
    # The legacy scalar above remains readable for Pass 1/2/3. New production
    # findings preserve every independently evidenced programmatic surface.
    api_surface_types: list[ApiSurfaceType] = Field(default_factory=list)
    api_breadth_notes: str | None = None
    api_surface_breadth: ApiSurfaceBreadth = "unknown"
    api_surface_summary: str | None = None
    mcp_exists: bool | None = None
    mcp_status: McpStatus = "unknown"
    mcp_notes: str | None = None
    buildability_verdict: Verdict | None = None
    # `blocker` is product evidence only; transport/model failures live below.
    blocker: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    needs_human_review: bool = False
    mcp_fallback: bool = True
    model_used: str = "unavailable"
    research_status: ResearchStatus = "partial"
    research_blockers: list[str] = Field(default_factory=list)

    @field_validator("auth_methods", mode="before")
    @classmethod
    def normalize_auth_method_aliases(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        return [AUTH_METHOD_ALIASES.get(item, item) if isinstance(item, str) else item for item in value]

    @field_validator("auth_methods")
    @classmethod
    def unique_auth_methods(cls, value: list[AuthMethod]) -> list[AuthMethod]:
        return list(dict.fromkeys(value))

    @field_validator("api_surface_types")
    @classmethod
    def unique_api_surface_types(cls, value: list[ApiSurfaceType]) -> list[ApiSurfaceType]:
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def status_matches_research_state(self) -> "AppFinding":
        has_api = bool(self.api_surface_types) or self.api_surface_type != "unknown"
        unknown = self.one_liner is None or self.self_serve_status == "unknown" or not has_api or self.mcp_exists is None or self.buildability_verdict is None
        if unknown and self.research_status in {"complete", "grounded"}:
            raise ValueError("grounded findings cannot contain unknown required research dimensions")
        if self.research_status == "unresolved" and not self.research_blockers:
            raise ValueError("unresolved findings require a research blocker")
        return self


class VerificationResult(BaseModel):
    field_name: str
    pipeline_value: str
    verified_value: str
    match: bool
    notes: str | None = None


RESEARCH_FIELDS = ("one_liner", "auth_methods", "self_serve_status", "api_surface_types", "api_surface_breadth", "api_surface_summary", "mcp_status", "buildability_verdict")
