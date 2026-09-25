"""One-off MCP probe — not part of submission."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx2
from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from student_agent.contracts import Contracts
from student_agent.mcp_gateway import EvidenceGateway


async def main() -> None:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    endpoint = os.getenv("MCP_ENDPOINT", "").strip()
    key = os.getenv("COMPETITION_TEAM_API_KEY", "").strip()
    headers = {"Authorization": f"Bearer {key}"}
    timeout = httpx2.Timeout(300.0, connect=30.0, write=30.0, pool=30.0)
    contracts = Contracts(Path("contracts/schemas"))

    async with (
        httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client,
        streamable_http_client(endpoint, http_client=http_client) as (rs, ws),
        ClientSession(rs, ws) as session,
    ):
        await session.initialize()
        listed = await session.list_tools()
        for tool in listed.tools:
            schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None)
            print("TOOL", tool.name, json.dumps(schema, indent=2)[:800])
            print()

    gateway = EvidenceGateway  # type hint only
    async with connect_probe(endpoint, key, contracts) as g:
        case_id = "L3B_CASE_001"
        order_id = "af0bbb47f125381ce9f3597dc70ef07b"
        customer_id = "customer-597dc70ef07b"
        for tool, args in [
            ("get_order", {"order_id": order_id}),
            ("get_order", {"order_id": "candidate-001"}),
            ("get_customer_history", {"customer_unique_id": customer_id}),
            ("get_shipment_summary", {"order_id": order_id}),
            ("get_order_payments", {"order_id": order_id}),
            ("get_payment_timeline", {"order_id": order_id}),
            ("get_refund_timeline", {"order_id": order_id}),
            ("get_order_items", {"order_id": order_id}),
            ("get_sellers", {"order_id": order_id}),
            ("get_product_context", {"order_id": order_id}),
            ("get_policy", {"policy_version": "EC_POLICY_V2"}),
        ]:
            try:
                evidence = await g.call(tool, case_id=case_id, **args)
                print("OK", tool, args)
                print(json.dumps(evidence, indent=2)[:2000])
            except Exception as exc:
                print("FAIL", tool, args, exc)
                raw = await g._session.call_tool(  # noqa: SLF001
                    tool, arguments={"case_id": case_id, **args}
                )
                print("RAW is_error", raw.is_error, "content", raw.content)
                print("RAW structured", raw.structured_content)
            print("---")


from contextlib import asynccontextmanager
from collections.abc import AsyncIterator


@asynccontextmanager
async def connect_probe(
    endpoint: str, team_api_key: str, contracts: Contracts
) -> AsyncIterator[EvidenceGateway]:
    headers = {"Authorization": f"Bearer {team_api_key}"}
    timeout = httpx2.Timeout(300.0, connect=30.0, write=30.0, pool=30.0)
    async with (
        httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client,
        streamable_http_client(endpoint, http_client=http_client) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield EvidenceGateway(session, contracts)


if __name__ == "__main__":
    asyncio.run(main())
