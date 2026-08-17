"""Current session-based Composio MCP transport for research source pages."""
from __future__ import annotations

import os
import json
import re
import asyncio
from dataclasses import dataclass

from composio import Composio


class MissingComposioCapability(RuntimeError):
    pass


class ComposioOperationTimeout(RuntimeError):
    pass


MCP_OPERATION_TIMEOUT_SECONDS = 45


def _mcp_transport():
    """Import the Windows-dependent MCP transport only when it is used.

    Source selection and local pipeline tests should not depend on a live MCP
    client runtime. Actual Composio operations still fail clearly at the
    transport boundary if that runtime is unavailable.
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    return ClientSession, streamablehttp_client


@dataclass
class ComposioCapabilities:
    session_id: str
    tool_names: list[str]
    fetch_tool: str | None
    browser_tools: list[str]


class ComposioResearchMCP:
    """Discovers tools on a per-run hosted MCP endpoint; never uses composio.mcp.create."""
    def __init__(self, user_id: str = "scout100-research") -> None:
        api_key = os.getenv("COMPOSIO_API_KEY")
        if not api_key:
            raise MissingComposioCapability("COMPOSIO_API_KEY is not configured.")
        self._composio = Composio(api_key=api_key)
        self._session = self._composio.sessions.create(
            user_id=user_id,
            toolkits=["composio_search", "browser_tool"],
            preload={"tools": "all"},
            mcp=True,
        )
        if not getattr(self._session, "mcp", None):
            raise MissingComposioCapability("Composio session did not return an MCP endpoint.")

    async def capabilities(self) -> ComposioCapabilities:
        try:
            return await asyncio.wait_for(self._capabilities(), timeout=MCP_OPERATION_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            raise ComposioOperationTimeout("Composio MCP tool discovery timed out") from exc

    async def _capabilities(self) -> ComposioCapabilities:
        ClientSession, streamablehttp_client = _mcp_transport()
        async with streamablehttp_client(self._session.mcp.url, headers=self._session.mcp.headers) as (read, write, _):
            async with ClientSession(read, write) as client:
                await client.initialize()
                tools = (await client.list_tools()).tools
        names = [tool.name for tool in tools]
        fetch = next((name for name in names if "FETCH" in name and "URL" in name and ("CONTENT" in name or "PAGE" in name)), None)
        browser = [name for name in names if name.startswith("BROWSER_TOOL_")]
        return ComposioCapabilities(getattr(self._session, "session_id", "unknown"), names, fetch, browser)

    async def relevant_tool_schemas(self) -> dict[str, dict]:
        """Read live session schemas for search/fetch/browser selection; no names guessed."""
        ClientSession, streamablehttp_client = _mcp_transport()
        async with streamablehttp_client(self._session.mcp.url, headers=self._session.mcp.headers) as (read, write, _):
            async with ClientSession(read, write) as client:
                await client.initialize()
                tools = (await client.list_tools()).tools
        return {tool.name: (tool.inputSchema or {}) for tool in tools if "SEARCH" in tool.name or "FETCH" in tool.name or tool.name.startswith("BROWSER_TOOL_")}

    async def fetch_url_content(self, url: str) -> tuple[str, ComposioCapabilities]:
        pages, capabilities = await self.fetch_urls_content([url])
        if not pages:
            raise MissingComposioCapability(f"No extracted text was returned for {url}.")
        return pages[0][1], capabilities

    async def fetch_urls_content(self, urls: list[str]) -> tuple[list[tuple[str, str]], ComposioCapabilities]:
        try:
            return await asyncio.wait_for(self._fetch_urls_content(urls), timeout=MCP_OPERATION_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            raise ComposioOperationTimeout(f"Composio MCP fetch timed out for {urls[0]}") from exc

    async def _fetch_urls_content(self, urls: list[str]) -> tuple[list[tuple[str, str]], ComposioCapabilities]:
        ClientSession, streamablehttp_client = _mcp_transport()
        async with streamablehttp_client(self._session.mcp.url, headers=self._session.mcp.headers) as (read, write, _):
            async with ClientSession(read, write) as client:
                await client.initialize()
                tools = (await client.list_tools()).tools
                fetch_tool = next((tool for tool in tools if "FETCH" in tool.name and "URL" in tool.name and ("CONTENT" in tool.name or "PAGE" in tool.name)), None)
                names = [tool.name for tool in tools]
                capabilities = ComposioCapabilities(getattr(self._session, "session_id", "unknown"), names, fetch_tool.name if fetch_tool else None, [name for name in names if name.startswith("BROWSER_TOOL_")])
                if not fetch_tool:
                    raise MissingComposioCapability(f"No URL fetch capability in this Composio session. Available tools: {', '.join(names)}")
                schema = fetch_tool.inputSchema or {}
                properties = schema.get("properties", {})
                url_key = next((key for key in properties if key.lower() in {"url", "urls", "link"}), None)
                if not url_key:
                    raise MissingComposioCapability(f"{fetch_tool.name} is available but does not expose a URL-like input field: {list(properties)}")
                value = urls if properties[url_key].get("type") == "array" else urls[0]
                args = {url_key: value}
                if "text" in properties:
                    args["text"] = True
                if "max_characters" in properties:
                    args["max_characters"] = 12_000
                result = await client.call_tool(fetch_tool.name, args)
        text = "\n".join(block.text for block in result.content if hasattr(block, "text"))
        if result.isError or not text.strip():
            raise MissingComposioCapability(f"{fetch_tool.name} failed to return fetched content.")
        return extracted_pages_from_tool_output(text, urls), capabilities

    async def search_web(self, query: str) -> str:
        """Call the live session's web-search tool after discovering its schema."""
        try:
            return await asyncio.wait_for(self._search_web(query), timeout=MCP_OPERATION_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            raise ComposioOperationTimeout("Composio MCP web search timed out") from exc

    async def _search_web(self, query: str) -> str:
        ClientSession, streamablehttp_client = _mcp_transport()
        async with streamablehttp_client(self._session.mcp.url, headers=self._session.mcp.headers) as (read, write, _):
            async with ClientSession(read, write) as client:
                await client.initialize()
                tools = (await client.list_tools()).tools
                tool = next((item for item in tools if item.name == "COMPOSIO_SEARCH_WEB" and "query" in (item.inputSchema or {}).get("properties", {})), None)
                if not tool:
                    raise MissingComposioCapability("No schema-compatible web search tool was exposed by this Composio session.")
                result = await client.call_tool(tool.name, {"query": query})
        text = "\n".join(block.text for block in result.content if hasattr(block, "text"))
        if result.isError or not text.strip():
            raise MissingComposioCapability(f"{tool.name} failed to return search results.")
        return text


def urls_from_tool_output(text: str) -> list[str]:
    """Extract public URLs from opaque MCP text/JSON responses without trusting their claims."""
    urls = re.findall(r"https?://[^\s\"'<>\\]+", text)
    return list(dict.fromkeys(url.rstrip(".,;:)]}`") for url in urls))


def extracted_pages_from_tool_output(text: str, requested_urls: list[str]) -> list[tuple[str, str]]:
    """Decode Composio Search's JSON response and retain URL/text associations."""
    try:
        payload = json.loads(text)
        data = payload.get("data", {})
        message = str(data.get("composio_execution_message") or "")
        if "failed to fetch" in message.lower() or "url(s) failed" in message.lower():
            return []
        results = data.get("results", [])
        pages = [(str(row.get("id") or row.get("url") or requested_urls[0]), str(row["text"]).strip()) for row in results if row.get("text")]
        if pages:
            return pages
    except (json.JSONDecodeError, AttributeError, TypeError, KeyError):
        pass
    return [(requested_urls[0], text.strip())]
