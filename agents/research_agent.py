"""Evidence-first, multi-source research agent.

Every source is discovered/fetched through the current Composio session MCP.
Extraction is intentionally field-tolerant: an unsupported field becomes
unknown while independently evidenced fields survive validation.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from agents.composio_mcp import ComposioResearchMCP, urls_from_tool_output
from pipeline.schema import AUTH_METHOD_ALIASES, AppFinding, AppSeed, Evidence

AUTH = {"oauth2", "api_key", "basic", "token", "bearer_token", "personal_access_token", "private_app_token", "bot_token", "jwt", "service_account", "signed_request", "session_token", "client_credentials", "other"}
ACCESS = {"self_serve_free", "self_serve_trial", "self_serve_paid", "self_serve_account_required", "paid_plan_required", "admin_approval_required", "partner_gated", "partner_approval_required", "contact_sales", "enterprise_only", "unknown"}
SURFACE = {"rest", "graphql", "rest_and_graphql", "sdk_only", "other", "none_public", "unknown"}
SURFACE_TYPES = {"rest", "graphql", "soap", "grpc", "websocket", "webhooks", "sdk", "rpc", "other"}
VERDICTS = {"yes", "yes_with_caveats", "no"}
MATERIAL_FIELDS = {"one_liner", "auth_methods", "self_serve_status", "api_surface_type", "api_surface_types", "api_breadth_notes", "api_surface_breadth", "api_surface_summary", "mcp_status", "mcp_exists", "buildability_verdict", "blocker"}
LLM_USER_AGENT = "scout100/1.0"
KEYWORDS = ("oauth", "auth", "api key", "access token", "bearer", "rest", "graphql", "api", "pricing", "plan", "permission", "mcp", "model context")
PRODUCT_PURPOSE_TERMS = (
    "manage", "customer", "sales", "marketing", "service", "communication", "collaboration", "workflow",
    "message", "voice", "email", "engagement", "platform", "business", "commerce", "payment", "finance",
    "data", "analytics", "security", "cloud", "infrastructure", "software", "productivity", "project",
    "document", "content", "video", "design", "recruit", "support", "developer", "automation",
)
PRODUCT_PURPOSE_VERBS = (
    "helps", "enables", "lets", "allows", "connects", "organizes", "analyzes", "creates", "builds",
    "tracks", "schedules", "stores", "shares", "sells", "delivers", "automates", "protects", "hosts",
)


class LLMRequestError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"LLM HTTP {status}: {' '.join(body.split())[:600]}")
        self.status = status


class LLMResponseFormatError(ValueError):
    """The provider responded, but did not supply a usable JSON object."""


# Kept as an alias for existing diagnostics/imports while the client supports
# both Groq and OpenRouter's OpenAI-compatible APIs.
GroqRequestError = LLMRequestError


def _unresolved(seed: AppSeed, reason: str, evidence: list[Evidence] | None = None) -> AppFinding:
    return AppFinding(app_id=seed.id, category=seed.category, evidence=evidence or [], confidence=0.0, needs_human_review=True, mcp_fallback=False, model_used="unavailable", research_status="unresolved", research_blockers=[reason])


def _official_root(url: str) -> str:
    parts = (urlparse(url).hostname or "").lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else ""


def _is_official(url: str, root: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    host_labels = host.split(".")
    placeholder_host = host.startswith(("your.", "your-", "your_")) or any(
        label in {"yourdomain", "your-domain", "placeholder"} for label in host_labels
    )
    placeholder_path = any(token in path for token in ("yourdomain", "your-domain", "{domain}", "<domain>"))
    return parsed.scheme == "https" and not placeholder_host and not placeholder_path and (host == root or host.endswith("." + root)) and not path.endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".pdf", ".json", ".webp", ".avif"))


def _documentation_score(url: str, field: str = "") -> int:
    path = urlparse(url).path.lower()
    score = sum(token in path for token in ("docs", "developer", "api", "oauth", "auth", "reference", "pricing", "mcp", "credential"))
    penalty_terms = ("authorize", "method_family", "privacy", "login")
    if field != "self_serve_status":
        penalty_terms += ("terms",)
    score -= 6 * sum(token in path for token in penalty_terms)
    field_terms = {
        "one_liner": ("product", "platform", "overview", "what-is", "what_is", "solutions"),
        "auth_methods": ("oauth", "auth", "authentication", "token", "credential"),
        "self_serve_status": (
            "pricing", "plan", "billing", "fee", "cost", "credential", "signup", "account",
            "terms", "support", "help", "onboarding", "verify", "approval", "review", "developer",
        ),
        "api_surface_types": ("api", "reference", "graphql", "rest", "soap", "grpc", "websocket", "developer"),
        "api_breadth_notes": ("reference", "api", "object", "resource", "endpoint"),
        "mcp_status": ("mcp", "model-context", "model_context", "server"),
    }
    score += 3 * sum(term in path for term in field_terms.get(field, ()))
    if field == "one_liner":
        score -= 4 * sum(term in path for term in ("developer", "api", "mcp", "reference"))
    if field == "self_serve_status":
        score += 6 * sum(term in path for term in (
            "pricing", "plan", "billing", "fee", "cost", "signup", "sign-up", "trial", "account",
            "credential", "terms", "support", "help", "onboarding", "verify", "approval", "review",
        ))
        score -= 6 * sum(term in path for term in ("oauth", "authorize", "token"))
    return score


def _field_suitable(url: str, field: str) -> bool:
    """Keep a page from being nominated for an unrelated material claim."""
    host = (urlparse(url).hostname or "").lower()
    path = urlparse(url).path.lower()
    # Search results often include site chrome and template links.  They have
    # no claim-bearing page text and must never displace documentation.
    if path.endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".avif", ".css", ".js", ".woff", ".woff2")):
        return False
    if host.startswith(("your.", "your-", "your_")) or any(token in path for token in ("yourdomain", "your-domain", "{domain}", "<domain>")):
        return False
    # A developer/authentication endpoint is not product-purpose evidence just
    # because it happens to mention the product name.  This is deliberately a
    # generic URL-shape check; it contains no app-specific knowledge.
    if field == "one_liner":
        if host.startswith(("dev.", "developer.")) or any(term in path for term in ("api", "oauth", "auth", "reference", "mcp")):
            return False
        # An official documentation overview can explain the customer product;
        # an API/reference page cannot.  This preserves that distinction
        # without rejecting every product overview that happens to live under
        # a documentation path.
        if any(term in path for term in ("docs", "developer")) and not any(
            term in path for term in ("product", "platform", "overview", "about", "feature", "solution", "what-is", "what_is")
        ):
            return False
    if field in {"api_surface_types", "api_breadth_notes"} and any(term in path for term in ("oauth", "authorize", "token")):
        return False
    if field == "self_serve_status" and any(term in path for term in ("oauth", "authorize", "token")):
        return False
    if field == "auth_methods" and any(term in path for term in ("pricing", "plans")):
        return False
    return True


def _is_product_domain_alias(url: str, name: str) -> bool:
    """Recognize a clean product-domain alias for product descriptions.

    A seed may point at ``developer.vendor.example`` while the first-party
    product overview lives at ``product.example``.  The exact brand-domain
    match is intentionally narrow and applies only to description discovery;
    it never broadens API/auth/MCP trust to arbitrary search results.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    # A canonical seed may include a parenthetical owner or product qualifier.
    # The public product domain normally uses the primary brand token, not the
    # qualifier.  This is name-shape normalization, not an app-specific map.
    primary_name = re.sub(r"\s*\([^)]*\)", "", name).strip()
    compact_names = {
        "".join(char for char in candidate.lower() if char.isalnum())
        for candidate in (name, primary_name)
    }
    root = _official_root(url)
    label = root.split(".", 1)[0]
    return any(len(compact_name) >= 3 and (
        label == compact_name
        or any(label == prefix + compact_name for prefix in ("get", "use", "try", "join", "go"))
    ) for compact_name in compact_names)


def _is_community_repository(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and (parsed.hostname or "").lower() in {"github.com", "www.github.com", "gitlab.com", "www.gitlab.com"}


def _source_type(url: str) -> str:
    return "community_repository" if _is_community_repository(url) else "official_docs"


def _queries_for(seed: AppSeed, fields: tuple[str, ...]) -> list[tuple[str, str]]:
    name = seed.name
    templates = {
        "one_liner": f"{name} product platform overview official",
        "auth_methods": f"{name} API authentication OAuth API key token official documentation",
        "self_serve_status": f"{name} developer API credentials account pricing access official documentation",
        "api_surface_types": f"{name} API reference REST GraphQL SOAP gRPC WebSocket Webhooks official documentation",
        "api_breadth_notes": f"{name} API reference resources endpoints official documentation",
        "buildability_verdict": f"{name} public API developer integration permissions restrictions official documentation",
        "blocker": f"{name} API developer access limitations plans permissions approval official documentation",
        # This deliberately includes a community-repository path. A company-homepage
        # result is not enough to claim MCP availability.
        "mcp_status": f"{name} Model Context Protocol MCP server official documentation GitHub",
    }
    queries = [(field, templates[field]) for field in fields if field in templates]
    # A single OAuth page often cannot establish every toolkit credential type.
    # This second, still field-scoped query is independent and additive.
    if "auth_methods" in fields:
        queries.append(("auth_methods", f"{name} API authentication overview tokens keys official documentation"))
        queries.append(("auth_methods", f"{name} API key bearer bot token personal access token official documentation"))
    if "self_serve_status" in fields:
        queries.append(("self_serve_status", f"{name} API free pricing trial developer account official documentation"))
        queries.append(("self_serve_status", f"{name} developer create application credentials access official documentation"))
        queries.append(("self_serve_status", f"{name} API free of charge no cost developer access official documentation"))
        queries.append(("self_serve_status", f"{name} API pricing billing fees free official"))
        queries.append(("self_serve_status", f"{name} API terms conditions eligibility developer official"))
        queries.append(("self_serve_status", f"{name} developer signup verification approval app review official"))
        queries.append(("self_serve_status", f"{name} API support account onboarding credentials official"))
    if "one_liner" in fields:
        queries.append(("one_liner", f"what is {name} official platform overview"))
        queries.append(("one_liner", f"{name} official product features solutions overview"))
        queries.append(("one_liner", f"{name} official about product platform"))
    return queries


async def discover_sources(seed: AppSeed, client: ComposioResearchMCP, fields: tuple[str, ...] | None = None) -> tuple[list[str], list[str]]:
    """Discover field-scoped official sources, retaining multiple candidates.

    Search snippets remain untrusted.  They only nominate URLs; fetched page
    content and later per-value evidence validation determine what may be
    asserted.  Access keeps a broader bounded set across product, commercial,
    legal, and developer pages so a developer reference cannot crowd out the
    page that actually explains credential eligibility or charges.
    """
    root = _official_root(str(seed.hint_url))
    wanted = fields or ("one_liner", "auth_methods", "self_serve_status", "api_surface_types", "api_breadth_notes", "mcp_status")
    urls = [str(seed.hint_url)]
    # The seed is usually a developer URL.  A first-party root page is a
    # second, generic candidate for product/access language (for example an
    # explicitly free offering), without widening trust beyond that root.
    if root and any(field in {"one_liner", "self_serve_status"} for field in wanted):
        urls.append(f"https://{root}/")
    blockers: list[str] = []
    queries = _queries_for(seed, wanted)
    results = await asyncio.gather(*(client.search_web(query) for _, query in queries), return_exceptions=True)
    candidates_by_field: dict[str, list[str]] = {field: [] for field in wanted}
    for (field, _), result in zip(queries, results):
        try:
            if isinstance(result, Exception):
                raise result
            candidates = [
                url for url in urls_from_tool_output(result)
                if _is_official(url, root)
                # A seed's developer host can be on a parent/legacy domain
                # while its exact brand domain hosts product, access, or API
                # documentation.  The alias is deliberately label-based and
                # field evidence remains mandatory after fetch.
                or _is_product_domain_alias(url, seed.name)
                or (field == "mcp_status" and _is_community_repository(url))
            ]
            candidates = [url for url in candidates if _field_suitable(url, field)]
            if not candidates:
                blockers.append(f"source discovery found no eligible {field} documentation URL")
                continue
            candidates_by_field.setdefault(field, []).extend(candidates)
        except Exception as exc:
            blockers.append(f"source discovery failed for {field}: {type(exc).__name__}")
    for field in wanted:
        ranked = sorted(
            dict.fromkeys(candidates_by_field.get(field, [])),
            key=lambda url: _documentation_score(url, field), reverse=True,
        )
        # Credential availability is often documented away from the OAuth
        # page (pricing, developer portal, account setup, or terms).  Retain
        # a bounded set of independent official candidates for this one field
        # so a high-scoring auth page cannot crowd out the access evidence.
        urls.extend(ranked[:8] if field == "self_serve_status" else ranked[:4] if field == "one_liner" else ranked[:2])
    return list(dict.fromkeys(urls)), blockers


async def fetch_sources(urls: list[str], client: ComposioResearchMCP) -> tuple[list[tuple[str, str]], list[str]]:
    pages: list[tuple[str, str]] = []
    blockers: list[str] = []
    results = await asyncio.gather(*(client.fetch_urls_content([url]) for url in urls), return_exceptions=True)
    for url, result in zip(urls, results):
        try:
            if isinstance(result, Exception):
                raise result
            fetched, _ = result
            requested_pages = [(source_url, text) for source_url, text in fetched if source_url == url and text.strip()]
            if not requested_pages:
                blockers.append(f"fetch returned no usable page text for {url}")
            # Browser tools can include links embedded in a fetched page in
            # their response.  Only the requested document may become source
            # evidence; otherwise placeholders and unrelated assets pollute
            # field discovery.
            pages.extend(requested_pages)
        except Exception as exc:
            blockers.append(f"fetch failed for {url}: {type(exc).__name__}")
    return list(dict.fromkeys(pages)), blockers


def _relevant_excerpt(text: str, limit: int = 550) -> str:
    """Keep deterministic evidence-bearing windows instead of sending whole docs pages."""
    normalized = " ".join(text.split())
    windows = [normalized[:600]]
    lowered = normalized.lower()
    for keyword in KEYWORDS:
        start = lowered.find(keyword)
        if start >= 0:
            windows.append(normalized[max(0, start - 350): start + 1_000])
    return "\n…\n".join(dict.fromkeys(windows))[:limit]


def _compact_sources_for_extraction(
    sources: list[tuple[str, str]], focus_fields: tuple[str, ...] | None = None,
) -> list[tuple[str, str]]:
    """Choose one best fetched page per required field for a compact prompt.

    Discovery/fetch retain alternate pages for audit and later recovery.  The
    extraction context deliberately uses only the strongest field-scoped page
    per dimension, preventing duplicate source windows from multiplying token
    cost without adding independent evidence.
    """
    fields = focus_fields or (
        "one_liner", "auth_methods", "self_serve_status", "api_surface_types",
        "api_breadth_notes", "mcp_status",
    )
    selected: list[tuple[str, str]] = []
    selected_urls: set[str] = set()
    for field in fields:
        candidates = [item for item in sources if _field_suitable(item[0], field)]
        if not candidates:
            continue
        url, text = max(candidates, key=lambda item: _documentation_score(item[0], field))
        if url not in selected_urls:
            selected.append((url, text))
            selected_urls.add(url)
    # A source may be best for multiple fields. Include one remaining fetched
    # seed page only when no field-scoped candidate was usable.
    return selected or sources[:1]


def _prompt(seed: AppSeed, sources: list[tuple[str, str]], hint: str | None, focus_fields: tuple[str, ...] | None = None) -> str:
    """Build a compact, deterministic JSON-mode extraction prompt.

    Source IDs prevent the same long URLs appearing in both instructions and
    evidence items.  IDs are expanded back to fetched URLs before validation.
    """
    source_text = "\n\n".join(
        f"[S{index}] {url}\n{_relevant_excerpt(text)}"
        for index, (url, text) in enumerate(sources, start=1)
    )
    target = "all fields" if not focus_fields else ", ".join(focus_fields)
    if focus_fields:
        field_schemas = {
            "one_liner": '"one_liner":null',
            "auth_methods": '"auth_methods":[]',
            "self_serve_status": '"self_serve_status":"unknown"',
            "api_surface_types": '"api_surface_types":[]',
            "api_breadth_notes": '"api_breadth_notes":null,"api_surface_breadth":"unknown","api_surface_summary":null',
            "mcp_status": '"mcp_exists":null,"mcp_status":"unknown","mcp_notes":null',
            "buildability_verdict": '"buildability_verdict":null,"blocker":null',
            "blocker": '"blocker":null',
        }
        schema_fields = [
            field for field in focus_fields
            if field in field_schemas and not (field == "blocker" and "buildability_verdict" in focus_fields)
        ]
        target_schema = ",".join(field_schemas[field] for field in schema_fields)
        recovery_rules = []
        if "self_serve_status" in focus_fields:
            recovery_rules.append("Access means how a developer obtains credentials. Do not treat OAuth/API-key authentication as self-serve access. Cite only explicit signup, trial, plan, admin, partner, or sales language.")
        if "api_breadth_notes" in focus_fields or "api_surface_types" in focus_fields:
            recovery_rules.append("For API scope, name documented resources/actions in api_breadth_notes and keep API types separate. Do not cite an OAuth/token endpoint as API breadth evidence.")
        if "one_liner" in focus_fields:
            recovery_rules.append("one_liner must describe the customer product purpose, not integration/API capabilities.")
        if "mcp_status" in focus_fields:
            recovery_rules.append("Classify MCP only from an explicit MCP source; do not infer a negative from an unrelated page.")
        if "blocker" in focus_fields:
            recovery_rules.append("blocker must be a product or integration constraint, never an HTTP, timeout, parser, MCP, or LLM failure.")
        return f"""Use only the fetched sources below to recover these fields for {seed.name} ({seed.category}): {target}.
Return exactly this compact JSON object and no other keys:
{{{target_schema},"evidence":[]}}
Evidence items are {{"field":"one target field","value":"method-or-surface only when applicable","url":"S1","note":"short support"}}. Use only S1, S2, etc.
{' '.join(recovery_rules)} Unknown is allowed; never invent facts.

SOURCES:
{source_text}"""
    return f"""Use only the fetched sources below to create one compact JSON object for {seed.name} ({seed.category}).
Target: {target}. Unknown is allowed. Never infer missing facts or use an infrastructure error as a product conclusion.

Return exactly these keys:
{{"one_liner":null,"auth_methods":[],"self_serve_status":"unknown","api_surface_types":[],"api_breadth_notes":null,"api_surface_breadth":"unknown","api_surface_summary":null,"mcp_exists":null,"mcp_status":"unknown","mcp_notes":null,"buildability_verdict":null,"blocker":null,"evidence":[]}}

Rules: one_liner is one 15-30 word product-purpose sentence, not an API description. Include every evidenced auth method and API surface. Evidence items are {{"field":"...","value":"method-or-surface only when applicable","url":"S1","note":"short support"}}. Use only S1, S2, etc. Evidence fields: one_liner, auth_methods, self_serve_status, api_surface_types, api_breadth_notes, api_surface_breadth, api_surface_summary, mcp_status, mcp_exists, buildability_verdict, blocker. Auth values: oauth2, api_key, basic, token, bearer_token, personal_access_token, private_app_token, bot_token, jwt, service_account, signed_request, session_token, client_credentials, other. Access values: self_serve_free, self_serve_trial, self_serve_paid, self_serve_account_required, paid_plan_required, admin_approval_required, partner_gated, partner_approval_required, contact_sales, enterprise_only, unknown. API values: rest, graphql, soap, grpc, websocket, webhooks, sdk, rpc, other. MCP values: official, community, proof_of_concept, no_evidence, unknown; official/community/proof_of_concept require an explicit MCP source. API breadth: broad, moderate, narrow, unknown. Buildability must be exactly yes, yes_with_caveats, no, or null; never write a sentence in buildability_verdict.

SOURCES:
{source_text}"""


def _expand_source_ids(payload: object, sources: list[tuple[str, str]]) -> object:
    """Replace compact S1-style evidence references with exact fetched URLs."""
    if not isinstance(payload, dict):
        return payload
    aliases = {f"S{index}": url for index, (url, _) in enumerate(sources, start=1)}
    result = dict(payload)
    evidence = result.get("evidence")
    if not isinstance(evidence, list):
        return result
    expanded: list[object] = []
    for item in evidence:
        if not isinstance(item, dict):
            expanded.append(item)
            continue
        copy = dict(item)
        url = copy.get("url")
        if isinstance(url, str):
            candidate = url.strip().upper()
            # Some OpenAI-compatible models echo the displayed source number
            # ("1" or "[1]") instead of S1.  Decode both representations;
            # accepting them is parser recovery, not a new source of truth.
            numeric = re.fullmatch(r"\[?(?:S)?(\d+)\]?", candidate)
            if candidate in aliases:
                copy["url"] = aliases[candidate]
            elif numeric and f"S{numeric.group(1)}" in aliases:
                copy["url"] = aliases[f"S{numeric.group(1)}"]
        expanded.append(copy)
    result["evidence"] = expanded
    return result


def _call_llm_raw(prompt: str, max_tokens_override: int | None = None, *, json_mode: bool = True) -> str:
    return _call_llm_response(prompt, max_tokens_override, json_mode=json_mode)[0]


def _provider_usage(payload: dict, requested_model: str) -> dict[str, int | float | str | None]:
    """Normalize optional OpenAI-compatible usage metadata for trace audit."""
    raw = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    details = raw.get("completion_tokens_details") if isinstance(raw.get("completion_tokens_details"), dict) else {}

    def number(*names: str) -> int | float | None:
        for name in names:
            value = raw.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return value
            value = details.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return value
        return None

    def cost(*names: str) -> int | float | None:
        for name in names:
            value = raw.get(name, payload.get(name))
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return value
        return None

    return {
        "input_tokens": number("prompt_tokens", "input_tokens"),
        "output_tokens": number("completion_tokens", "output_tokens"),
        "reasoning_tokens": number("reasoning_tokens"),
        "cost": cost("cost", "total_cost"),
        "credits": cost("credits", "credit_cost"),
        "model": payload.get("model") if isinstance(payload.get("model"), str) else requested_model,
    }


def _call_llm_response(prompt: str, max_tokens_override: int | None = None, *, json_mode: bool = True) -> tuple[str, dict[str, int | float | str | None]]:
    provider = os.getenv("LLM_PROVIDER", "groq").lower()
    if provider == "openrouter":
        key = os.getenv("OPENROUTER_API_KEY")
        model = os.getenv("LLM_MODEL", "z-ai/glm-5.2")
        endpoint = "https://openrouter.ai/api/v1/chat/completions"
        headers = {"HTTP-Referer": "https://scout100.local", "X-Title": "Scout100"}
        max_tokens = int(os.getenv("LLM_MAX_COMPLETION_TOKENS", "4096"))
        reasoning = {"enabled": False} if os.getenv("LLM_DISABLE_REASONING", "").lower() == "true" else {"effort": "high", "exclude": True}
        token_parameter = "max_completion_tokens"
    elif provider == "aicredits":
        key = os.getenv("AICREDITS_API_KEY")
        model = os.getenv("LLM_MODEL", "z-ai/glm-5.2")
        base_url = os.getenv("LLM_BASE_URL", "https://api.aicredits.in/v1").rstrip("/")
        endpoint = f"{base_url}/chat/completions"
        headers = {}
        max_tokens = int(os.getenv("LLM_MAX_COMPLETION_TOKENS", "4096"))
        reasoning = None
        # AICredits exposes the OpenAI-compatible completion limit name.
        token_parameter = "max_tokens"
    elif provider == "groq":
        key = os.getenv("GROQ_API_KEY")
        model = os.getenv("GROQ_MODEL", "groq/compound")
        endpoint = "https://api.groq.com/openai/v1/chat/completions"
        headers = {}
        max_tokens = int(os.getenv("LLM_MAX_COMPLETION_TOKENS", "1200"))
        reasoning = None
        token_parameter = "max_completion_tokens"
    else:
        raise RuntimeError(f"Unsupported LLM_PROVIDER: {provider}")
    if not key:
        raise RuntimeError(f"{provider.upper()} API key is not configured")
    if max_tokens_override is not None:
        max_tokens = max_tokens_override
    # Bound both evidence context and completion size: field recovery should not
    # consume the entire daily model budget and starve later apps.
    payload = {"model": model, "messages": [{"role": "system", "content": "Return concise valid JSON only."}, {"role": "user", "content": prompt}], "temperature": 0, token_parameter: max_tokens}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if reasoning:
        payload["reasoning"] = reasoning
    body = json.dumps(payload).encode()
    request = Request(endpoint, data=body, method="POST", headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": LLM_USER_AGENT, **headers})
    try:
        with urlopen(request, timeout=90) as response:  # nosec - fixed provider endpoint
            decoded = json.loads(response.read().decode())
            if not isinstance(decoded.get("choices"), list) or not decoded["choices"]:
                raise LLMRequestError(200, json.dumps(decoded))
            choice = decoded["choices"][0]
            message = choice.get("message") if isinstance(choice, dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            # Some OpenAI-compatible providers encode message content as typed
            # blocks rather than one string.  Concatenate text blocks only;
            # non-text blocks are never interpreted as research evidence.
            if isinstance(content, list):
                content = "".join(
                    item if isinstance(item, str) else item.get("text", "")
                    for item in content
                    if isinstance(item, str) or (isinstance(item, dict) and isinstance(item.get("text"), str))
                )
            if not isinstance(content, str) or not content.strip():
                raise LLMRequestError(200, f"{provider} returned no final content (finish_reason={choice.get('finish_reason')})")
            return content, _provider_usage(decoded, model)
    except HTTPError as error:
        raise LLMRequestError(error.code, error.read().decode(errors="replace")) from error


def _decode_json_object(raw: str) -> dict:
    """Extract exactly one complete JSON object from compatible chat output.

    This accepts harmless prose or Markdown fencing around a complete object,
    but never repairs or invents malformed JSON values.  A malformed response
    remains a parser failure for the bounded retry/recovery path.
    """
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else ""
        cleaned = cleaned.rsplit("```", 1)[0].strip()
    decoder = json.JSONDecoder()
    for index, character in enumerate(cleaned):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(cleaned[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise LLMResponseFormatError("provider content did not contain a complete JSON object")


def _call_llm(prompt: str, max_tokens_override: int | None = None, *, json_mode: bool = True) -> dict:
    raw = _call_llm_raw(prompt, json_mode=json_mode) if max_tokens_override is None else _call_llm_raw(prompt, max_tokens_override, json_mode=json_mode)
    return _decode_json_object(raw)


def _call_llm_with_usage(prompt: str, max_tokens_override: int | None = None, *, json_mode: bool = True) -> tuple[dict, dict[str, int | float | str | None]]:
    raw, usage = _call_llm_response(prompt, max_tokens_override, json_mode=json_mode)
    return _decode_json_object(raw), usage


def _is_product_description(value: str) -> bool:
    """Reject integration-first prose while allowing product-purpose wording."""
    words = value.split()
    lowered = value.lower()
    if not 10 <= len(words) <= 36:
        return False
    first_clause = lowered.split(".", 1)[0]
    opening = " ".join(first_clause.split()[:10])
    integration_first = any(phrase in first_clause for phrase in ("developer api", "developer platform", "api platform", "provides mcp", "allows developers", "api reference documentation")) or ("api" in opening and any(verb in opening for verb in ("provides", "offers", "build", "access")))
    product_purpose = any(term in lowered for term in PRODUCT_PURPOSE_TERMS) or any(
        term in lowered for term in PRODUCT_PURPOSE_VERBS
    )
    return product_purpose and not integration_first


def _access_source_classification(seed: AppSeed, source_text_by_url: dict[str, str]) -> tuple[str, list[Evidence]] | None:
    """Derive credential access only from explicit first-party source language.

    This prevents a model omission from hiding documentation that directly
    states a free, trial, paid, approval-gated, or enterprise access path.
    The match requires credential/API/developer context where relevant and is
    intentionally independent of the app name.
    """
    root = _official_root(str(seed.hint_url))
    free_source: tuple[str, str] | None = None
    self_service_source: tuple[str, str] | None = None
    usage_pricing_source: tuple[str, str] | None = None
    automatic_access_source: tuple[str, str] | None = None
    for url, text in source_text_by_url.items():
        if not _is_official(url, root):
            continue
        lowered = " ".join(text.lower().split())
        if any(term in lowered for term in (
            "free of charge", "without charge", "no charge", "at no cost", "free tier",
            "available to all developers", "available to any developer", "open to all developers",
        )):
            free_source = (url, "Official source describes a free offering.")
        api_context = any(term in lowered for term in ("api", "developer", "application", "app", "credentials", "access token", "bot"))
        if not api_context:
            continue
        if any(term in lowered for term in (
            "per-message", "usage-based", "usage based", "per request", "per-api-call",
            "are charged", "is charged", "charges businesses", "usage charges",
        )):
            usage_pricing_source = (url, "Official pricing documentation describes usage-based API charges.")
        if any(term in lowered for term in (
            "automatically approved", "automatic approval", "no app review", "no advanced access",
            "no approval required", "standard access", "create your application", "register an application",
        )):
            automatic_access_source = (url, "Official documentation describes self-service or automatically approved developer access.")
        status: str | None = None
        note: str | None = None
        if any(term in lowered for term in ("enterprise-only", "enterprise only", "only available on enterprise")):
            status, note = "enterprise_only", "Official documentation states that API access is enterprise-only."
        elif any(term in lowered for term in ("partner approval", "approved partner", "developer program approval")):
            status, note = "partner_approval_required", "Official documentation requires partner or developer-program approval."
        elif any(term in lowered for term in (
            "admin approval", "administrator approval", "approved by an admin", "workspace admin",
            "requires admin access", "requires administrator access", "administrator must enable",
        )):
            status, note = "admin_approval_required", "Official documentation requires administrator approval."
        elif any(term in lowered for term in ("contact sales", "talk to sales", "sales team")):
            status, note = "contact_sales", "Official documentation directs developers to contact sales for access."
        elif any(term in lowered for term in ("free trial", "trial account", "start a trial")):
            status, note = "self_serve_trial", "Official documentation offers a self-serve trial for developer access."
        elif any(term in lowered for term in ("paid plan required", "requires a paid plan", "available on paid plans", "paid subscription required")):
            status, note = "paid_plan_required", "Official documentation requires a paid plan for developer access."
        elif any(term in lowered for term in ("sign up for a paid", "subscribe to a paid", "purchase a plan")):
            status, note = "self_serve_paid", "Official documentation describes self-serve paid developer access."
        elif any(term in lowered for term in (
            "api is free", "free api", "free to use", "free of charge", "without charge",
            "no charge", "no cost", "at no cost", "free tier", "available to all developers",
            "available to any developer", "open to all developers",
        )):
            status, note = "self_serve_free", "Official documentation states a free self-serve API or developer access path."
        if status:
            return status, [Evidence(field="self_serve_status", value=status, url=url, note=note, source_type="official_docs")]
        if any(term in lowered for term in (
            "create an application", "create a new application", "create your application",
            "register an application", "developer account", "create an app", "register your app",
        )):
            self_service_source = (url, "Official developer documentation permits self-service application or credential setup.")
    # A free first-party offering alone does not prove API credential access,
    # and an app-creation page alone does not prove it is free.  Together they
    # are a generic, evidence-backed self-serve-free path with two provenance
    # records rather than a guess based on product popularity.
    if free_source and self_service_source:
        free_url, free_note = free_source
        setup_url, setup_note = self_service_source
        return "self_serve_free", [
            Evidence(field="self_serve_status", value="self_serve_free", url=free_url, note=free_note, source_type="official_docs"),
            Evidence(field="self_serve_status", value="self_serve_free", url=setup_url, note=setup_note, source_type="official_docs"),
        ]
    # A usage-priced API can still be self-service to obtain credentials.  We
    # require both a commercial source and a separate explicit self-service /
    # automatic-access source, so pricing by itself is never mistaken for an
    # access restriction.
    if usage_pricing_source and automatic_access_source:
        price_url, price_note = usage_pricing_source
        access_url, access_note = automatic_access_source
        return "self_serve_paid", [
            Evidence(field="self_serve_status", value="self_serve_paid", url=price_url, note=price_note, source_type="official_docs"),
            Evidence(field="self_serve_status", value="self_serve_paid", url=access_url, note=access_note, source_type="official_docs"),
        ]
    if self_service_source:
        setup_url, setup_note = self_service_source
        return "self_serve_account_required", [
            Evidence(field="self_serve_status", value="self_serve_account_required", url=setup_url, note=setup_note, source_type="official_docs"),
        ]
    return None


def _canonical_token(value: object) -> str | None:
    """Normalize a provider's casing/spacing without broadening its meaning."""
    if not isinstance(value, str):
        return None
    token = re.sub(r"[\s\-]+", "_", value.strip().lower())
    aliases = {
        "basic_auth": "basic",
        "apikey": "api_key",
        "bearer": "bearer_token",
        "bot": "bot_token",
        "webhook": "webhooks",
        "web_socket": "websocket",
        "restful": "rest",
        "rest_api": "rest",
        "graphql_api": "graphql",
        "grpc_api": "grpc",
    }
    return aliases.get(token, token)


_SOURCE_VALUE_MARKERS: dict[str, tuple[str, ...]] = {
    "oauth2": ("oauth2", "oauth 2", "oauth 2.0"),
    "api_key": ("api key", "apikey", "api_key", "x-api-key"),
    "basic": ("basic auth", "basic authentication"),
    "token": ("access token", "authentication token"),
    "bearer_token": ("bearer token", "bearer authentication", "authorization: bearer", "authorization bearer"),
    "personal_access_token": ("personal access token",),
    "private_app_token": ("private app token",),
    "bot_token": ("bot token",),
    "jwt": (" json web token", "jwt"),
    "service_account": ("service account",),
    "signed_request": ("signed request",),
    "session_token": ("session token",),
    "client_credentials": ("client credentials",),
    "rest": ("rest api", "restful api", "rest endpoint"),
    "graphql": ("graphql",),
    "soap": ("soap api", "soap service"),
    "grpc": ("grpc",),
    "websocket": ("websocket", "web socket"),
    "webhooks": ("webhook", "web hook"),
    "sdk": ("sdk", "software development kit"),
    "rpc": ("rpc", "remote procedure call"),
}


def _source_value_evidence(field: str, values: list[str], source_text_by_url: dict[str, str]) -> list[Evidence]:
    """Recover explicitly named auth/API values directly from fetched docs."""
    recovered: list[Evidence] = []
    suitable_field = "auth_methods" if field == "auth_methods" else "api_surface_types"
    for value in values:
        markers = _SOURCE_VALUE_MARKERS.get(value, ())
        for url, text in source_text_by_url.items():
            if not _field_suitable(url, suitable_field):
                continue
            if any(marker in text.lower() for marker in markers):
                recovered.append(Evidence(
                    field=field, value=value, url=url,
                    note="Explicitly named in fetched first-party documentation.",
                    source_type=_source_type(url),
                ))
                break
    return recovered


def _fallback_description_evidence(value: str, source_text_by_url: dict[str, str]) -> Evidence | None:
    """Cite the highest-ranked fetched product page for a valid product summary."""
    candidates = [
        (url, text) for url, text in source_text_by_url.items()
        if text.strip() and _field_suitable(url, "one_liner")
    ]
    if not candidates:
        return None
    url, _ = max(candidates, key=lambda item: _documentation_score(item[0], "one_liner"))
    return Evidence(
        field="one_liner", url=url,
        note="Valid product-purpose summary grounded in the fetched product overview.",
        source_type=_source_type(url),
    )


def _fallback_breadth_evidence(value: str, source_text_by_url: dict[str, str]) -> Evidence | None:
    """Accept a concise breadth summary only when its resource terms occur in API docs."""
    words = {
        word for word in re.findall(r"[a-z]{4,}", value.lower())
        if word not in {"broad", "moderate", "narrow", "api", "apis", "with", "from", "that", "this", "and", "the", "for"}
    }
    if not words:
        return None
    for url, text in sorted(source_text_by_url.items(), key=lambda item: _documentation_score(item[0], "api_breadth_notes"), reverse=True):
        if not _field_suitable(url, "api_breadth_notes"):
            continue
        if len(words.intersection(re.findall(r"[a-z]{4,}", text.lower()))) >= 2:
            return Evidence(
                field="api_breadth_notes", url=url,
                note="Resource terms in the breadth summary occur in fetched API documentation.",
                source_type=_source_type(url),
            )
    return None


def _source_breadth_summary(source_text_by_url: dict[str, str]) -> tuple[str, Evidence] | None:
    """Derive a compact breadth summary from explicit API-reference headings."""
    for url, text in sorted(source_text_by_url.items(), key=lambda item: _documentation_score(item[0], "api_breadth_notes"), reverse=True):
        if not _field_suitable(url, "api_breadth_notes"):
            continue
        headings = re.findall(r"^#{2,4}\s+([^\n#]{3,90})", text, flags=re.MULTILINE)
        headings = [
            " ".join(heading.split()) for heading in headings
            if not any(term in heading.lower() for term in ("authentication", "authorization", "getting started", "pagination", "errors"))
        ]
        unique = list(dict.fromkeys(headings))[:5]
        if len(unique) >= 2:
            summary = "Documented API reference covers " + ", ".join(unique[:-1]) + ", and " + unique[-1] + "."
            return summary, Evidence(
                field="api_breadth_notes", url=url,
                note="Multiple named resources/actions occur in fetched API-reference headings.",
                source_type=_source_type(url),
            )
    return None


def _meaningful_text(value: object) -> str | None:
    """Return substantive model text, never an omitted-field sentinel.

    ``Unknown`` is a provider placeholder, not a claim.  Treating it as a
    populated breadth field previously prevented deterministic recovery from
    already-fetched, official API-reference pages.
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped or stripped.casefold() in {"unknown", "n/a", "na", "null", "none", "not available"}:
        return None
    return stripped


def normalize_payload(seed: AppSeed, payload: object, source_urls: set[str], research_blockers: list[str], mcp_fallback: bool, mcp_source_urls: set[str] | None = None, source_text_by_url: dict[str, str] | None = None) -> AppFinding:
    """Keep only independently evidenced, schema-valid fields from an LLM response."""
    raw = payload if isinstance(payload, dict) else {}
    evidence: list[Evidence] = []
    # Canonicalize only semantically equivalent field labels emitted by a
    # provider.  This protects evidence provenance without guessing values.
    field_aliases = {
        "self_serve_access": "self_serve_status",
        "credential_access": "self_serve_status",
        "developer_access": "self_serve_status",
        "access": "self_serve_status",
        "api_surface": "api_surface_types",
    }
    for item in raw.get("evidence", []) if isinstance(raw.get("evidence"), list) else []:
        if not isinstance(item, dict):
            continue
        raw_field = _canonical_token(item.get("field"))
        item = {**item, "field": field_aliases.get(raw_field, raw_field)}
        if item.get("field") not in MATERIAL_FIELDS or item.get("url") not in source_urls:
            continue
        if item.get("field") == "one_liner" and not _field_suitable(str(item["url"]), "one_liner"):
            research_blockers.append("discarded one_liner evidence from a developer or integration page")
            continue
        if item.get("field") in {"mcp_status", "mcp_exists"} and mcp_source_urls is not None and item.get("url") not in mcp_source_urls:
            continue
        try:
            normalized_item = {**item, "source_type": _source_type(str(item["url"]))}
            if normalized_item.get("field") in {"auth_methods", "api_surface_types", "api_surface_type", "self_serve_status"}:
                normalized_item["value"] = _canonical_token(normalized_item.get("value"))
                if normalized_item.get("field") == "auth_methods":
                    normalized_item["value"] = AUTH_METHOD_ALIASES.get(normalized_item["value"], normalized_item["value"])
            evidence.append(Evidence.model_validate(normalized_item))
        except Exception:
            research_blockers.append("discarded malformed evidence item")

    raw_one_liner = _meaningful_text(raw.get("one_liner"))
    raw_methods = [_canonical_token(item) for item in raw.get("auth_methods", [])] if isinstance(raw.get("auth_methods"), list) else []
    raw_methods = [AUTH_METHOD_ALIASES.get(item, item) for item in raw_methods if item in AUTH]
    raw_surfaces = [_canonical_token(item) for item in raw.get("api_surface_types", [])] if isinstance(raw.get("api_surface_types"), list) else []
    raw_surfaces = [item for item in raw_surfaces if item in SURFACE_TYPES]
    legacy_surface = _canonical_token(raw.get("api_surface_type"))
    legacy_surface = legacy_surface if legacy_surface in SURFACE else "unknown"

    # A provider occasionally omits source IDs even while returning a value.
    # Recover only when the exact value is explicitly named in a field-suitable
    # fetched page.  This is evidence recovery, never a model-knowledge guess.
    if source_text_by_url:
        if raw_one_liner and _is_product_description(raw_one_liner) and not any(item.field == "one_liner" for item in evidence):
            fallback = _fallback_description_evidence(raw_one_liner, source_text_by_url)
            if fallback:
                evidence.append(fallback)
        auth_candidates = raw_methods or list(AUTH - {"other"})
        api_candidates = raw_surfaces or list(SURFACE_TYPES - {"other"})
        for item in _source_value_evidence("auth_methods", auth_candidates, source_text_by_url):
            if not any(old.field == item.field and old.value == item.value for old in evidence):
                evidence.append(item)
        for item in _source_value_evidence("api_surface_types", api_candidates, source_text_by_url):
            if not any(old.field == item.field and old.value == item.value for old in evidence):
                evidence.append(item)
        raw_breadth = _meaningful_text(raw.get("api_breadth_notes"))
        if raw_breadth and not any(item.field == "api_breadth_notes" for item in evidence):
            fallback = _fallback_breadth_evidence(raw_breadth, source_text_by_url)
            if fallback:
                evidence.append(fallback)
        if not raw_breadth and not any(item.field == "api_breadth_notes" for item in evidence):
            source_breadth = _source_breadth_summary(source_text_by_url)
            if source_breadth:
                raw["api_breadth_notes"], fallback = source_breadth
                evidence.append(fallback)
    evidence_fields = {item.field for item in evidence}
    auth_was_empty = "auth_methods" not in raw or (isinstance(raw.get("auth_methods"), list) and not raw.get("auth_methods"))
    surfaces_were_empty = "api_surface_types" not in raw or (isinstance(raw.get("api_surface_types"), list) and not raw.get("api_surface_types"))
    if not raw_methods and auth_was_empty:
        raw_methods = [
            item.value for item in evidence
            if item.field == "auth_methods" and isinstance(item.value, str) and item.value in AUTH
        ]
    if not raw_surfaces and surfaces_were_empty:
        raw_surfaces = [
            item.value for item in evidence
            if item.field == "api_surface_types" and isinstance(item.value, str) and item.value in SURFACE_TYPES
        ]
    supported = lambda name: name in evidence_fields
    def supports_value(field: str, value: str, candidates: list[str]) -> bool:
        exact = any(item.field == field and item.value == value for item in evidence)
        # Legacy traces pre-date per-item provenance. Preserve their single
        # field value, but fail closed for a new multi-value claim without a
        # matching item-level source.
        legacy_single = len(candidates) == 1 and any(item.field == field and item.value is None for item in evidence)
        return exact or legacy_single
    one_liner = raw_one_liner if supported("one_liner") else None
    if one_liner and not _is_product_description(one_liner):
        one_liner = None
        research_blockers.append("discarded one_liner that did not describe the product purpose in one reviewer-friendly sentence")
    methods = raw_methods
    auth_methods = [item for item in methods if supports_value("auth_methods", item, methods)]
    raw_access = _canonical_token(raw.get("self_serve_status"))
    self_serve = raw_access if raw_access in ACCESS and supported("self_serve_status") else "unknown"
    if self_serve != "unknown" and source_text_by_url:
        access_urls = [item.url.unicode_string() for item in evidence if item.field == "self_serve_status"]
        access_text = " ".join(source_text_by_url.get(url, "") for url in access_urls).lower()
        support_tokens = {
            "self_serve_free": ("free", "sign up", "signup"),
            "self_serve_trial": ("free trial", "trial", "sign up", "signup"),
            "self_serve_paid": ("paid", "pricing", "subscription", "purchase"),
            "self_serve_account_required": ("create an account", "sign up", "signup", "register", "account required"),
            "paid_plan_required": ("paid", "pricing", "subscription", "price"),
            "admin_approval_required": ("admin", "administrator", "approval"),
            "partner_gated": ("partner", "contact sales", "approval"),
            "partner_approval_required": ("partner", "contact sales", "approval"),
            "contact_sales": ("contact sales", "talk to sales", "sales team"),
            "enterprise_only": ("enterprise", "contact sales", "enterprise plan"),
        }
        if not any(token in access_text for token in support_tokens[self_serve]):
            self_serve = "unknown"
            research_blockers.append("discarded self_serve_status without explicit access-language support in cited source")
    if self_serve == "unknown" and source_text_by_url:
        source_access = _access_source_classification(seed, source_text_by_url)
        if source_access:
            self_serve, access_evidence = source_access
            for item in access_evidence:
                if not any(old.field == "self_serve_status" and old.url == item.url for old in evidence):
                    evidence.append(item)
    # Read legacy one-value payloads while emitting the new array for every
    # fresh run.
    if not raw_surfaces and legacy_surface not in {"unknown", "none_public", "sdk_only", "rest_and_graphql"}:
        raw_surfaces = [legacy_surface]
    api_surface_types = [item for item in raw_surfaces if supports_value("api_surface_types", item, raw_surfaces)]
    if not api_surface_types and legacy_surface not in {"unknown", "none_public", "sdk_only", "rest_and_graphql"} and supported("api_surface_type"):
        api_surface_types = [legacy_surface]
    if set(api_surface_types) == {"rest", "graphql"}:
        surface = "rest_and_graphql"
    elif len(api_surface_types) == 1 and api_surface_types[0] in {"rest", "graphql", "other"}:
        surface = api_surface_types[0]
    elif api_surface_types:
        surface = "other"
    else:
        surface = legacy_surface if legacy_surface in {"none_public", "sdk_only"} and supported("api_surface_type") else "unknown"
    breadth = _meaningful_text(raw.get("api_breadth_notes")) if supported("api_breadth_notes") else None
    def supports_mcp_status(value: str, field: str) -> bool:
        return any(
            item.field == field and (item.value == value or item.value is None)
            for item in evidence
        )

    mcp_status = raw.get("mcp_status") if (
        raw.get("mcp_status") in {"official", "community", "proof_of_concept"}
        and supports_mcp_status(raw["mcp_status"], "mcp_status")
    ) else "unknown"
    # Models occasionally put the classification in mcp_exists rather than
    # the boolean/status pair.  Recover that schema-shape error only when the
    # exact classification has field-level evidence from an MCP source.
    legacy_mcp_status = raw.get("mcp_exists")
    if mcp_status == "unknown" and isinstance(legacy_mcp_status, str) and legacy_mcp_status in {"official", "community", "proof_of_concept"} and supports_mcp_status(legacy_mcp_status, "mcp_exists"):
        mcp_status = legacy_mcp_status
    if mcp_status != "unknown" and isinstance(raw.get("mcp_notes"), str) and any(term in raw["mcp_notes"].lower() for term in ("proof of concept", "proof-of-concept", "experimental")):
        mcp_status = "proof_of_concept"
    if mcp_status == "unknown" and source_text_by_url:
        source_classification = _mcp_source_classification(seed, source_text_by_url)
        if source_classification:
            mcp_status, mcp_note, mcp_evidence = source_classification
            if not any(item.field == "mcp_status" and item.url == mcp_evidence.url for item in evidence):
                evidence.append(mcp_evidence)
            raw = {**raw, "mcp_notes": mcp_note}
    # No-MCP evidence requires a stronger, auditable negative-research protocol
    # than a silent document page. Keep it unresolved instead of inventing a no.
    mcp = True if mcp_status in {"official", "community", "proof_of_concept"} else None
    verdict = raw.get("buildability_verdict") if supported("buildability_verdict") and raw.get("buildability_verdict") in VERDICTS else None
    blocker = raw.get("blocker") if verdict is not None and supported("blocker") and isinstance(raw.get("blocker"), str) else None

    # Buildability is a reviewer conclusion from product evidence; public
    # documentation rarely contains the word "buildable".  When a documented
    # programmatic surface and a documented authentication path survive the
    # evidence gate, derive the conclusion from those already-grounded facts.
    # This is generic, conservative, and never turns a transport failure into
    # a product claim.
    if verdict is None and (api_surface_types or surface in {"rest", "graphql", "rest_and_graphql", "other"}) and auth_methods:
        restricted_access = {"paid_plan_required", "admin_approval_required", "partner_gated", "partner_approval_required", "contact_sales"}
        verdict = "yes_with_caveats" if self_serve == "unknown" or self_serve in restricted_access else "yes"
        api_evidence = next((item for item in evidence if item.field in {"api_surface_types", "api_surface_type"}), None)
        auth_evidence = next((item for item in evidence if item.field == "auth_methods"), None)
        basis = api_evidence or auth_evidence
        if basis and not any(item.field == "buildability_verdict" for item in evidence):
            evidence.append(Evidence(
                field="buildability_verdict", url=basis.url,
                note="Derived from independently evidenced public API and authentication support.",
                source_type=basis.source_type,
            ))
        if blocker is None and verdict == "yes_with_caveats":
            blocker = "Credential access unresolved" if self_serve == "unknown" else "Credential or approval requirements apply"
    known = {"one_liner": one_liner, "auth_methods": auth_methods or None, "self_serve_status": None if self_serve == "unknown" else self_serve, "api_surface_types": api_surface_types or (surface if surface in {"none_public", "sdk_only"} else None), "api_breadth_notes": breadth, "mcp_status": None if mcp_status == "unknown" else mcp_status, "buildability_verdict": verdict}
    missing = [name for name, value in known.items() if value is None]
    research_blockers.extend(f"no grounded evidence for {name}" for name in missing)
    research_blockers = list(dict.fromkeys(research_blockers))
    status = "grounded" if not missing and not research_blockers else ("partial" if any(value is not None for value in known.values()) else "unresolved")
    breadth_level = raw.get("api_surface_breadth") if supported("api_surface_breadth") and raw.get("api_surface_breadth") in {"broad", "moderate", "narrow"} else "unknown"
    summary = _meaningful_text(raw.get("api_surface_summary")) if supported("api_surface_summary") else breadth
    return AppFinding(app_id=seed.id, category=seed.category, one_liner=one_liner, auth_methods=auth_methods, auth_other_label=raw.get("auth_other_label") if "other" in auth_methods and isinstance(raw.get("auth_other_label"), str) else None, self_serve_status=self_serve, gating_reason=raw.get("gating_reason") if self_serve != "unknown" and isinstance(raw.get("gating_reason"), str) else None, api_surface_type=surface, api_surface_types=api_surface_types, api_breadth_notes=breadth, api_surface_breadth=breadth_level, api_surface_summary=summary, mcp_exists=mcp, mcp_status=mcp_status, mcp_notes=raw.get("mcp_notes") if mcp is not None and isinstance(raw.get("mcp_notes"), str) else None, buildability_verdict=verdict, blocker=blocker, evidence=evidence, confidence=round(len(set(known) - set(missing)) / len(known), 2), needs_human_review=status != "grounded", mcp_fallback=mcp_fallback, model_used=os.getenv("LLM_MODEL") or os.getenv("GROQ_MODEL", "groq/compound"), research_status=status, research_blockers=research_blockers)


def _missing_fields(finding: AppFinding) -> tuple[str, ...]:
    fields: list[str] = []
    if finding.one_liner is None: fields.append("one_liner")
    if not finding.auth_methods: fields.append("auth_methods")
    if finding.self_serve_status == "unknown": fields.append("self_serve_status")
    if not finding.api_surface_types and finding.api_surface_type == "unknown": fields.append("api_surface_types")
    if finding.api_breadth_notes is None: fields.append("api_breadth_notes")
    if finding.mcp_status == "unknown": fields.append("mcp_status")
    if finding.buildability_verdict is None: fields.append("buildability_verdict")
    return tuple(fields)


def _merge_recovery(first: dict, recovery: dict, missing: tuple[str, ...]) -> dict:
    """Add only evidence-supported values for previously missing fields."""
    merged = dict(first)
    recovery_evidence = recovery.get("evidence") if isinstance(recovery.get("evidence"), list) else []
    accepted = [item for item in recovery_evidence if isinstance(item, dict) and item.get("field") in set(missing)]
    accepted_fields = {item["field"] for item in accepted}
    for field in missing:
        if field in accepted_fields and field in recovery:
            merged[field] = recovery[field]
    if "mcp_status" in accepted_fields:
        merged["mcp_exists"] = recovery.get("mcp_exists")
        merged["mcp_notes"] = recovery.get("mcp_notes")
    merged["evidence"] = [*(first.get("evidence") if isinstance(first.get("evidence"), list) else []), *accepted]
    return merged


def _mcp_mention_sources(sources: list[tuple[str, str]]) -> set[str]:
    """Only an explicit MCP mention may support an MCP availability claim."""
    return {
        url for url, text in sources
        if "model context protocol" in text.lower() or "mcp server" in text.lower()
    }


def _mcp_source_classification(seed: AppSeed, source_text_by_url: dict[str, str]) -> tuple[str, str, Evidence] | None:
    """Classify explicit MCP source evidence without relying on an LLM label.

    The rule is intentionally source-based rather than app-based: a first-party
    page that explicitly presents an MCP server is an official offering unless
    its own text marks it experimental; a repository with explicit MCP wording
    is community evidence.  A mere product homepage or a search-result absence
    still produces no MCP claim.
    """
    root = _official_root(str(seed.hint_url))
    for url, text in source_text_by_url.items():
        lowered = text.lower()
        path = urlparse(url).path.lower()
        explicit = "model context protocol" in lowered or "mcp server" in lowered or "mcp" in path
        if not explicit:
            continue
        if _is_official(url, root):
            experimental = any(term in lowered for term in ("proof of concept", "proof-of-concept", "experimental", "open beta", "beta"))
            status = "proof_of_concept" if experimental else "official"
            note = "First-party documentation explicitly presents an MCP server."
            if experimental:
                note = "First-party documentation presents an experimental MCP server."
            return status, note, Evidence(field="mcp_status", value=status, url=url, note=note, source_type="official_docs")
        if _is_community_repository(url):
            return "community", "Community repository explicitly implements an MCP server.", Evidence(
                field="mcp_status", value="community", url=url,
                note="Community repository explicitly implements an MCP server.",
                source_type="community_repository",
            )
    return None


async def _extract_payload(seed: AppSeed, sources: list[tuple[str, str]], hint: str | None, focus_fields: tuple[str, ...] | None = None, *, capture_usage: bool = False, json_mode: bool = True) -> dict | tuple[dict, dict[str, int | float | str | None]]:
    compact_sources = _compact_sources_for_extraction(sources, focus_fields)
    budget_name = "LLM_RECOVERY_MAX_TOKENS" if focus_fields else "LLM_INITIAL_MAX_TOKENS"
    default_budget = "900" if focus_fields else "1600"
    prompt = _prompt(seed, compact_sources, hint, focus_fields)
    budget = int(os.getenv(budget_name, default_budget))
    worker = _call_llm_with_usage if capture_usage else _call_llm
    payload = await asyncio.to_thread(worker, prompt, budget) if json_mode else await asyncio.to_thread(worker, prompt, budget, json_mode=False)
    if capture_usage:
        extracted, usage = payload
        if not isinstance(extracted, dict):
            raise ValueError("LLM response was not a JSON object")
        return _expand_source_ids(extracted, compact_sources), usage
    if not isinstance(payload, dict):
        raise ValueError("LLM response was not a JSON object")
    return _expand_source_ids(payload, compact_sources)


async def research_app(seed: AppSeed, hint: str | None = None, rendered: bool = False, use_composio: bool = True, composio_client: ComposioResearchMCP | None = None) -> AppFinding:
    if rendered or not use_composio:
        return _unresolved(seed, "primary research requires the Composio MCP source-discovery path")
    try:
        client = composio_client or ComposioResearchMCP()
        urls, blockers = await discover_sources(seed, client)
        sources, fetch_blockers = await fetch_sources(urls, client)
        blockers.extend(fetch_blockers)
    except Exception as exc:
        return _unresolved(seed, f"Composio MCP research setup failed: {type(exc).__name__}")
    if not sources:
        return _unresolved(seed, "No official documentation source could be fetched after discovery.")
    try:
        payload = await _extract_payload(seed, sources, hint)
        all_sources = list(sources)
        finding = normalize_payload(seed, payload, {url for url, _ in all_sources}, list(blockers), mcp_fallback=False, mcp_source_urls=_mcp_mention_sources(all_sources), source_text_by_url=dict(all_sources))
        missing = _missing_fields(finding)
        if missing:
            try:
                recovery_urls, recovery_discovery_blockers = await discover_sources(seed, client, missing)
                recovery_sources, recovery_fetch_blockers = await fetch_sources(recovery_urls, client)
                blockers.extend(recovery_discovery_blockers + recovery_fetch_blockers)
            except Exception as exc:
                recovery_sources = []
                blockers.append(f"field recovery discovery/fetch failed: {type(exc).__name__}")
            # A focused, small-context retry is intentionally separate. It cannot
            # erase first-pass evidence even if a later URL or model call fails.
            if recovery_sources:
                try:
                    recovery = await _extract_payload(seed, recovery_sources, hint, missing)
                    payload = _merge_recovery(payload, recovery, missing)
                    all_sources.extend(item for item in recovery_sources if item[0] not in {url for url, _ in all_sources})
                except Exception as exc:
                    blockers.append(f"field recovery extraction failed: {type(exc).__name__}")
        return normalize_payload(seed, payload, {url for url, _ in all_sources}, blockers, mcp_fallback=False, mcp_source_urls=_mcp_mention_sources(all_sources), source_text_by_url=dict(all_sources))
    except Exception as exc:
        evidence = [Evidence(field="source_page", url=url, note="Fetched source available for retry.") for url, _ in sources]
        return _unresolved(seed, f"LLM extraction failed after official sources were fetched: {type(exc).__name__}", evidence)
