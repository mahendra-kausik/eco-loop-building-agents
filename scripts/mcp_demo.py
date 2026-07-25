"""Proves an LLM/MCP client can drive the loop through MCP (spec + demo video
material): connects to src/mcp_server/server.py over stdio, lists the 5 tools,
and calls each one in sequence against whatever run is newest under
results/raw/.

Run baseline or agent run first so there's a state.csv to read:
    python scripts/run_baseline.py
    python scripts/mcp_demo.py
"""
import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "src", "mcp_server", "server.py")


async def main() -> None:
    params = StdioServerParameters(command=sys.executable, args=[SERVER_SCRIPT])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print(f"Tools available: {[t.name for t in tools.tools]}\n")

            print("-- get_building_state --")
            state = await session.call_tool("get_building_state", {})
            print(state.content[0].text, "\n")

            print("-- get_forecast_context --")
            forecast = await session.call_tool("get_forecast_context", {"horizon": 6})
            print(forecast.content[0].text, "\n")

            print("-- get_recent_errors --")
            errors = await session.call_tool("get_recent_errors", {"n": 5})
            print(errors.content[0].text, "\n")

            print("-- inject_setpoints (21.0 heating, 24.5 cooling, occupied) --")
            injected = await session.call_tool(
                "inject_setpoints", {"heating_c": 21.0, "cooling_c": 24.5, "occupied": True}
            )
            print(injected.content[0].text, "\n")

            print("-- propose_setpoints (calls the configured LLM; needs an API key) --")
            try:
                proposal = await session.call_tool("propose_setpoints", {})
                print(proposal.content[0].text)
            except Exception as exc:  # noqa: BLE001 -- demo tolerance, not the loop's safety path
                print(f"propose_setpoints failed (expected if no LLM key set): {exc}")


if __name__ == "__main__":
    asyncio.run(main())
