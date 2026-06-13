"""
MCP smoke test (§1 isolation check).

Requires the server to be running:
    python incident_mcp_server/main.py

Run:
    python incident_mcp_server/smoke_test.py
"""
import asyncio
import json
import time

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

SERVER = "http://127.0.0.1:8002/mcp"

OK = "[OK]"


def check(label: str, value):
    print(f"  {OK} {label}: {json.dumps(value, ensure_ascii=False)[:120]}")


def parse_list(r) -> list:
    """FastMCP v1.x serialises each element of a list tool return as a separate
    TextContent item — collect and parse them all."""
    return [json.loads(c.text) for c in r.content]


def parse_single(r) -> dict | list:
    """Parse a single-item TextContent response."""
    return json.loads(r.content[0].text) if r.content else {}


async def main():
    print(f"\nConnecting to {SERVER} ...")

    async with streamablehttp_client(SERVER) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print(f"{OK} Session initialized\n")

            # ── list tools ─────────────────────────────────────────────
            tools_result = await session.list_tools()
            tool_names = [t.name for t in tools_result.tools]
            check("tools/list", tool_names)
            expected = {"search_similar_incidents", "record_incident",
                        "get_recent_incidents", "get_stats"}
            assert expected == set(tool_names), f"Missing tools: {expected - set(tool_names)}"

            # ── record_incident ────────────────────────────────────────
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            r = await session.call_tool("record_incident", {
                "ts": ts,
                "symptoms": ["high_latency", "high_cpu"],
                "metrics_snapshot": {"latency_ms": 2500, "cpu_pct": 91},
                "diagnosis": "Overload spike (smoke test)",
                "action": "restart_service",
                "outcome": "Service recovered in 45 s",
                "resolved": True,
            })
            rec = parse_single(r)
            check("record_incident", rec)
            assert "id" in rec

            # ── get_recent_incidents ───────────────────────────────────
            r = await session.call_tool("get_recent_incidents", {"n": 5})
            recent = parse_list(r)
            check(f"get_recent_incidents (got {len(recent)})", [i["id"] for i in recent])
            assert len(recent) >= 1

            # ── search_similar_incidents ───────────────────────────────
            r = await session.call_tool("search_similar_incidents", {
                "symptoms": ["high_latency"],
                "limit": 3,
            })
            hits = parse_list(r)
            check(f"search_similar_incidents (got {len(hits)} hit(s))",
                  [h.get("diagnosis", "") for h in hits])

            # ── get_stats ──────────────────────────────────────────────
            r = await session.call_tool("get_stats", {})
            stats = parse_single(r)
            check("get_stats", stats)
            assert stats["total"] >= 1

            # ── resources ─────────────────────────────────────────────
            print()
            resources = await session.list_resources()
            uris = [str(res.uri) for res in resources.resources]
            check("resources/list", uris)
            assert "incidents://recent" in uris
            assert "incidents://stats" in uris

            r_recent = await session.read_resource("incidents://recent")
            data = json.loads(r_recent.contents[0].text)
            check(f"incidents://recent ({len(data)} item(s))", [i.get("id") for i in data[:3]])

            r_stats = await session.read_resource("incidents://stats")
            data = json.loads(r_stats.contents[0].text)
            check("incidents://stats", data)

            print(f"\n{OK} All MCP checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
