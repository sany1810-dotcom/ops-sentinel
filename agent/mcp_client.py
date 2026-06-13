"""
Synchronous MCP client for the incident-memory server (Week 2, §2).

Wraps the async MCP SDK in a background event loop so the agent thread
can call it synchronously. Each tool call opens a short-lived session —
simple and resilient (no session expiry issues).

Falls back gracefully: if the server is unreachable, `available` is False
and callers revert to Week 1 direct-memory behaviour.
"""
import asyncio
import json
import logging
import threading
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

logger = logging.getLogger(__name__)


def _to_openai_schema(tool) -> dict:
    """Convert an MCP Tool object to OpenAI function-calling format."""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.inputSchema or {"type": "object", "properties": {}},
        },
    }


class IncidentMCPClient:
    """
    Thread-safe synchronous wrapper around the async MCP Streamable-HTTP client.
    """

    def __init__(self, server_url: str):
        self._url = server_url
        self.available: bool = False
        self.tool_schemas: list[dict] = []

        # Dedicated event loop running in a background daemon thread
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="mcp-event-loop"
        )
        self._loop_thread.start()

        self._probe()

    # ── internal helpers ───────────────────────────────────────────────────

    def _run(self, coro, timeout: float = 15.0) -> Any:
        """Submit a coroutine to the background loop and block until done."""
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    async def _list_tools(self) -> list[dict]:
        async with streamablehttp_client(self._url) as (read, write, _):
            async with ClientSession(read, write) as sess:
                await sess.initialize()
                result = await sess.list_tools()
                return [_to_openai_schema(t) for t in result.tools]

    async def _call_tool(self, name: str, args: dict) -> Any:
        async with streamablehttp_client(self._url) as (read, write, _):
            async with ClientSession(read, write) as sess:
                await sess.initialize()
                result = await sess.call_tool(name, args)
                if not result.content:
                    return None
                # FastMCP v1.x: each element of a list return → separate TextContent
                if len(result.content) == 1:
                    return json.loads(result.content[0].text)
                return [json.loads(c.text) for c in result.content]

    # ── public API ─────────────────────────────────────────────────────────

    def _probe(self) -> None:
        """Try to connect and fetch tool list. Sets available flag."""
        try:
            self.tool_schemas = self._run(self._list_tools())
            self.available = True
            logger.info("MCP server connected — %d tools: %s",
                        len(self.tool_schemas),
                        [t["function"]["name"] for t in self.tool_schemas])
        except Exception as exc:
            self.available = False
            logger.warning("MCP server unreachable (%s) — using Week 1 fallback", exc)

    def reconnect(self) -> bool:
        """Attempt to reconnect after a previous failure. Returns new available state."""
        self._probe()
        return self.available

    def call_tool(self, name: str, args: dict, timeout: float = 15.0) -> Any:
        """
        Call an MCP tool synchronously.
        Raises on network/protocol errors — callers should catch and fall back.
        """
        return self._run(self._call_tool(name, args), timeout=timeout)
