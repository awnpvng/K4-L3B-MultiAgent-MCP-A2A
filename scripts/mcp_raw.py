import asyncio
import json
import os
from pathlib import Path

import httpx2
from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


async def main() -> None:
    endpoint = os.getenv("MCP_ENDPOINT", "").strip()
    key = os.getenv("COMPETITION_TEAM_API_KEY", "").strip()
    headers = {"Authorization": f"Bearer {key}"}
    async with (
        httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(60.0)) as hc,
        streamable_http_client(endpoint, http_client=hc) as (rs, ws),
        ClientSession(rs, ws) as session,
    ):
        await session.initialize()
        result = await session.call_tool(
            "get_order",
            arguments={
                "case_id": "L3B_CASE_001",
                "order_id": "af0bbb47f125381ce9f3597dc70ef07b",
            },
        )
        print(json.dumps(result.model_dump(), indent=2, default=str))


asyncio.run(main())
