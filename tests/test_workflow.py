from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from student_agent.contracts import Contracts
from student_agent.evidence import CaseEvidenceStore, ToolPermissionError
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = Contracts(ROOT / "contracts" / "schemas")
ORDER = "af0bbb47f125381ce9f3597dc70ef07b"
CUSTOMER = "customer-597dc70ef07b"
DOMAINS = {
    "get_order": "order",
    "get_order_items": "item",
    "get_order_payments": "payment",
    "get_payment_timeline": "payment",
    "get_refund_timeline": "refund",
    "get_shipment_summary": "shipment",
    "get_sellers": "seller",
    "get_product_context": "product",
    "get_customer_history": "customer",
    "get_policy": "policy",
}


class FakeGateway:
    """Serves canned payloads in the MCP evidence envelope and records every call."""

    def __init__(self, payloads: dict[str, Any]) -> None:
        self.payloads = payloads
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.issued: set[str] = set()

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, arguments))
        key = arguments.get("order_id") or arguments.get("customer_unique_id") or ""
        if tool_name not in self.payloads or key.startswith("candidate-"):
            raise RuntimeError(f"MCP tool {tool_name} failed: not found")
        digest = hashlib.sha256(f"{case_id}:{tool_name}:{key}".encode()).hexdigest()
        evidence = {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{digest[:32]}",
            "result_hash": f"sha256:{digest}",
            "domain": DOMAINS[tool_name],
            "data": self.payloads[tool_name],
        }
        CONTRACTS.validate_evidence(evidence)
        self.issued.add(evidence["evidence_ref"])
        return evidence


def make_case(topic: str) -> dict[str, Any]:
    return {
        "case_id": "L3B_CASE_001",
        "opened_at": "2018-01-01T09:00:00-03:00",
        "customer_request": {
            "language": "vi",
            "message": "test",
            "claimed_order_id": ORDER,
            "claims": [
                {"claim_id": "claim-001-a", "topic": topic},
                {"claim_id": "claim-001-b", "topic": "requested_full_refund"},
            ],
        },
        "policy_version": "EC_POLICY_V2",
        "candidate_order_ids": [ORDER, "candidate-001"],
        "investigation_scope": {},
        "customer_unique_id_hint": CUSTOMER,
    }


def base_payloads() -> dict[str, Any]:
    return {
        "get_customer_history": {
            "customer_unique_id": CUSTOMER,
            "orders": [{"order_id": ORDER}, {"order_id": "0" * 32}],
        },
        "get_order": {
            "order_id": ORDER,
            "customer_unique_id": CUSTOMER,
            "order_status": "delivered",
            "order_purchase_timestamp": "2017-12-01 10:00:00",
            "order_delivered_carrier_date": "2017-12-03 10:00:00",
            "order_delivered_customer_date": "2017-12-10 10:00:00",
            "order_estimated_delivery_date": "2017-12-20 00:00:00",
        },
        "get_policy": {"policy_version": "EC_POLICY_V2"},
        "get_shipment_summary": {
            "shipment_id": "shp-1",
            "sellers": [{"seller_id": "seller-1", "shipping_limit_date": "2017-12-05 00:00:00"}],
        },
        "get_payment_timeline": {
            "payments": [{"payment_sequential": 1, "payment_value": 100.0}],
            "events": [
                {
                    "event_type": "capture",
                    "status": "succeeded",
                    "amount": 100.0,
                    "payment_reference": "pay-1",
                },
            ],
        },
        "get_refund_timeline": {"events": []},
        "get_order_items": {
            "items": [
                {
                    "order_id": ORDER,
                    "order_item_id": 1,
                    "seller_id": "seller-1",
                    "price": 80.0,
                    "freight_value": 20.0,
                    "shipping_limit_date": "2017-12-05 00:00:00",
                },
            ]
        },
    }


def run(case: dict[str, Any], gateway: FakeGateway, tmp_path: Path) -> tuple[dict, list[dict]]:
    trace_path = tmp_path / "trace.jsonl"
    trace = TraceWriter(trace_path, CONTRACTS)
    trace.emit(case_id=case["case_id"], event_type="case_received", actor="coordinator")
    output = asyncio.run(solve_case(case, gateway, trace))  # type: ignore[arg-type]
    trace.emit(case_id=case["case_id"], event_type="case_finalized", actor="coordinator")
    CONTRACTS.validate_output(output, "output")
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    return output, events


def assert_common(output: dict, events: list[dict], gateway: FakeGateway) -> None:
    types = {event["event_type"] for event in events}
    assert {
        "case_received",
        "task_assigned",
        "handoff",
        "verification_completed",
        "case_finalized",
    } <= types
    assert events[0]["event_type"] == "case_received"
    assert events[-1]["event_type"] == "case_finalized"
    assert set(output["evidence_refs"]) <= gateway.issued
    consumed = {
        ref
        for e in events
        if e["event_type"] == "tool_result_consumed"
        for ref in e["evidence_refs"]
    }
    assert set(output["evidence_refs"]) <= consumed
    assert len(gateway.calls) == len(set((t, tuple(sorted(a.items()))) for t, a in gateway.calls))


def test_late_seller_delivery(tmp_path: Path) -> None:
    payloads = base_payloads()
    payloads["get_order"]["order_delivered_carrier_date"] = "2017-12-08 10:00:00"
    payloads["get_order"]["order_delivered_customer_date"] = "2017-12-25 10:00:00"
    gateway = FakeGateway(payloads)
    output, events = run(make_case("late_delivery_seller"), gateway, tmp_path)
    assert_common(output, events, gateway)
    assert output["assessment"]["primary_issue"] == "late_delivery_seller"
    assert output["shipment_analysis"]["late_seller_ids"] == ["seller-1"]
    assert output["entity_resolution"]["status"] == "resolved"
    assert output["entity_resolution"]["rejected_candidates"] == ["candidate-001"]
    parties = output["root_cause_analysis"]["responsible_parties"]
    assert parties == [{"party_type": "seller", "party_id": "seller-1"}]
    # The decoy is rejected from customer history without an extra get_order call.
    assert ("get_order", {"order_id": "candidate-001"}) not in gateway.calls


def test_duplicate_charge_refunds_duplicate_only(tmp_path: Path) -> None:
    payloads = base_payloads()
    payloads["get_payment_timeline"]["events"].append(
        {
            "event_type": "capture",
            "status": "succeeded",
            "amount": 100.0,
            "payment_reference": "pay-1",
        }
    )
    gateway = FakeGateway(payloads)
    output, events = run(make_case("duplicate_charge"), gateway, tmp_path)
    assert_common(output, events, gateway)
    assert output["assessment"]["primary_issue"] == "duplicate_charge"
    assert output["payment_analysis"]["verdict"] == "duplicate_capture"
    assert output["financial_resolution"]["recommended_refund_brl"] == 100.0
    assert output["assessment"]["case_status"] == "action_required"
    # On-time delivery: no item call needed.
    assert all(tool != "get_order_items" for tool, _ in gateway.calls)


def test_unsupported_claim_is_no_action(tmp_path: Path) -> None:
    gateway = FakeGateway(base_payloads())
    output, events = run(make_case("late_delivery_logistics"), gateway, tmp_path)
    assert_common(output, events, gateway)
    assert output["assessment"]["primary_issue"] == "unsupported_claim"
    assert output["assessment"]["case_status"] == "no_action"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0
    assert output["resolution_actions"] == []


def test_missing_evidence_needs_investigation(tmp_path: Path) -> None:
    gateway = FakeGateway({})
    output, events = run(make_case("refund_failed"), gateway, tmp_path)
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert output["entity_resolution"]["status"] == "not_found"
    assert output["evidence_refs"] == []


def test_least_privilege(tmp_path: Path) -> None:
    trace = TraceWriter(tmp_path / "t.jsonl", CONTRACTS)
    store = CaseEvidenceStore("L3B_CASE_001", FakeGateway({}), trace)  # type: ignore[arg-type]
    with pytest.raises(ToolPermissionError):
        asyncio.run(store.fetch("shipment-agent", "get_payment_timeline", order_id=ORDER))
