"""Non-persisting boundary trace for one Scout100 research run."""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agents.composio_mcp import ComposioResearchMCP
from agents.research_agent import LLMRequestError, LLMResponseFormatError, _extract_payload, _mcp_mention_sources, _unresolved, discover_sources, fetch_sources, normalize_payload
from pipeline.env_loader import load_dotenv
from pipeline.schema import AppFinding, AppSeed
from pipeline.storage import list_seeds

load_dotenv()


def _sanitized_error(exc: Exception) -> str:
    text = re.sub(r"(Bearer\s+|sk-or-[\w-]+|gsk_[\w-]+|ak_)[^\s,;]+", r"\1[REDACTED]", str(exc))
    return " ".join(text.split())[:600]


def execution_failure_category(exc: Exception) -> str:
    """Classify an execution failure without claiming a product limitation."""
    if isinstance(exc, LLMRequestError) and exc.status == 200 and "no final content" in str(exc).lower():
        return "provider_empty_content"
    if isinstance(exc, LLMRequestError):
        return {402: "provider_402", 429: "provider_429"}.get(exc.status, "provider_failure")
    if isinstance(exc, (LLMResponseFormatError, json.JSONDecodeError)):
        return "parser_failure"
    text = str(exc).lower()
    if "no final content" in text:
        return "provider_empty_content"
    if "unhashable type" in text:
        return "normalization_failure"
    if "jsondecode" in text or "json decode" in text:
        return "parser_failure"
    if "validationerror" in text:
        return "validation_failure"
    if "composio" in text or "mcp" in text or "session" in text:
        return "composio_session_failure"
    if "urlerror" in type(exc).__name__.lower() or "connection" in text or "timeout" in text:
        return "provider_network_failure"
    return "extraction_failure"


def _validation_output(finding: AppFinding, execution_failure: str | None = None) -> dict:
    output = {
        "status": "execution_failure" if execution_failure else "success",
        "research_status": finding.research_status,
        "evidence_fields": [item.field for item in finding.evidence],
        "research_blockers": finding.research_blockers,
        "buildability_verdict": finding.buildability_verdict,
        "finding": finding.model_dump(mode="json"),
    }
    if execution_failure:
        output["execution_failure"] = execution_failure
    return output

async def trace(app_name: str, client: ComposioResearchMCP | None = None, source_only: bool = False) -> dict:
    seed = next((item for item in list_seeds(ROOT / "data/research.db") if item.name.casefold() == app_name.casefold()), None)
    if not seed:
        raise SystemExit(f"Unknown seeded app: {app_name}")
    output = {"app": seed.name, "seed_url": str(seed.hint_url)}
    try:
        client = client or ComposioResearchMCP()
        capabilities = await client.capabilities()
        output["mcp_catalog"] = {"status": "success", "fetch_tool": capabilities.fetch_tool, "browser_tool_count": len(capabilities.browser_tools)}
        urls, discovery_blockers = await discover_sources(seed, client)
        output["source_discovery"] = {"status": "partial" if discovery_blockers else "success", "official_urls": urls, "blockers": discovery_blockers}
        sources, fetch_blockers = await fetch_sources(urls, client)
        output["fetch"] = {"status": "partial" if fetch_blockers else "success", "pages": [{"url": url, "chars": len(text), "preview": text[:180]} for url, text in sources], "blockers": fetch_blockers}
    except Exception as exc:
        category = execution_failure_category(exc)
        output["run_failure"] = {"status": "failure", "category": category, "error": _sanitized_error(exc)}
        output["validation"] = _validation_output(_unresolved(seed, f"research_execution_failure:{category}"), category)
        return output
    if not sources:
        output["validation"] = _validation_output(_unresolved(seed, "research_execution_failure:fetch_failure"), "fetch_failure")
        return output
    if source_only:
        # A preflight intentionally exercises the same MCP discovery/fetch
        # boundaries while issuing no provider request and persisting nothing.
        output["preflight"] = {"status": "success", "llm_called": False, "fetched_page_count": len(sources)}
        return output
    try:
        fallback_used = False
        try:
            payload, usage = await _extract_payload(seed, sources, None, capture_usage=True)
        except LLMRequestError as exc:
            # A route that acknowledges JSON mode but emits an empty message
            # gets exactly one transport fallback. The prompt still demands
            # JSON, and the parser/evidence checks remain unchanged.
            if execution_failure_category(exc) != "provider_empty_content":
                raise
            payload, usage = await _extract_payload(seed, sources, None, capture_usage=True, json_mode=False)
            fallback_used = True
        # Raw model JSON is safe, append-only diagnostic data. It lets us
        # distinguish a model omission from a later normalization rejection.
        output["llm"] = {"status": "success", "fallback_used": fallback_used, "fields_returned": sorted(payload), "evidence_count": len(payload.get("evidence", [])) if isinstance(payload.get("evidence"), list) else 0, "usage": usage, "payload": payload}
        all_sources = list(sources)
        blockers = discovery_blockers + fetch_blockers
        # The generic runner performs the sole targeted recovery, if required.
        # Keeping this trace to one initial extraction prevents a standalone
        # trace recovery plus a runner recovery from spending twice for the
        # same missing field.
        finding = normalize_payload(seed, payload, {url for url, _ in all_sources}, blockers, False, _mcp_mention_sources(all_sources), dict(all_sources))
        output["validation"] = _validation_output(finding)
    except Exception as exc:
        category = execution_failure_category(exc)
        output["llm_or_validation"] = {"status": "failure", "category": category, "error": f"{type(exc).__name__}: {_sanitized_error(exc)}"}
        output["validation"] = _validation_output(_unresolved(seed, f"research_execution_failure:{category}"), category)
    return output

def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--app", required=True); parser.add_argument("--out"); parser.add_argument("--source-only", action="store_true")
    args = parser.parse_args()
    rendered = json.dumps(asyncio.run(trace(args.app, source_only=args.source_only)), indent=2)
    if args.out:
        Path(args.out).write_text(rendered, encoding="utf-8")
    print(rendered)

if __name__ == "__main__": main()
