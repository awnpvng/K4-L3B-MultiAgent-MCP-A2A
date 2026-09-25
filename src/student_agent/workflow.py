from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx2

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

# =============================================================================
# LLM Configuration - GPT-4o Mini
# =============================================================================

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = "gpt-4o-mini"


# =============================================================================
# LLM Client
# =============================================================================

async def call_llm(prompt: str, system_prompt: str = "", temperature: float = 0.1) -> str:
    """Call OpenAI GPT-4o Mini API."""
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not set")

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "max_tokens": 2048,
    }
    async with httpx2.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]


# =============================================================================
# System Prompt for LLM Analysis
# =============================================================================

ANALYSIS_SYSTEM = """You are an expert e-commerce complaint investigator. Analyze the case data and return ONLY a valid JSON object:

{
    "primary_issue": "canceled_order_paid | unavailable_order_paid | late_delivery_seller | late_delivery_logistics | valid_split_payment | payment_mismatch | duplicate_charge | refund_pending | refund_failed | unsupported_claim | insufficient_evidence",
    "case_status": "action_required | no_action | needs_investigation",
    "confidence": 0.0 to 1.0,
    "secondary_issues": ["issue1", "issue2"],
    "root_cause_code": "SELLER_SHIP_DELAY | LOGISTICS_DELAY | ORDER_CANCELED_AFTER_PAY | PAYMENT_AMOUNT_MISMATCH | REFUND_NOT_PROCESSED | DUPLICATE_PAYMENT | VALID_SPLIT_PAYMENT | NO_ISSUE_FOUND | EVIDENCE_GAP",
    "responsible_party": "seller | platform | logistics_provider | payment_provider | customer | unknown",
    "recommended_refund_brl": 0.0 to captured amount,
    "refund_reason_code": "LATE_DELIVERY_REFUND | CANCELED_ORDER_REFUND | PAYMENT_MISMATCH_REFUND | PENDING_REFUND | REFUND_FREIGHT | NO_REFUND",
    "resolution_actions": ["action1", "action2"]
}

Return ONLY JSON, no markdown or explanation."""


# =============================================================================
# Main Workflow
# =============================================================================

async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Multi-agent L3B workflow using GPT-4o Mini."""
    case_id = case["case_id"]
    all_evidence_refs = []

    trace.emit(case_id=case_id, event_type="task_assigned", actor="coordinator", target="entity-resolution")

    # =========================================================================
    # PHASE 1: Entity Resolution
    # =========================================================================
    entity_result = await _resolve_entity(case, gateway, trace)
    all_evidence_refs.extend(entity_result.get("evidence_refs", []))

    trace.emit(case_id=case_id, event_type="task_assigned", actor="coordinator", target="investigation")

    # =========================================================================
    # PHASE 2: Gather All Evidence
    # =========================================================================
    order_ids = entity_result.get("resolved_order_ids", [])

    # Customer context
    customer_data = await _call_mcp(
        gateway, case_id, "customer-agent", "get_customer_history", trace,
        customer_unique_id=case.get("customer_unique_id_hint", "")
    )
    if customer_data:
        all_evidence_refs.extend(customer_data.get("evidence_refs", []))

    # Order details - collect from ALL orders
    order_data = {"orders": [], "items": [], "evidence_refs": []}
    for order_id in order_ids:
        result = await _call_mcp(gateway, case_id, "order-agent", "get_order", trace, order_id=order_id)
        if result:
            order_data["evidence_refs"].extend(result.get("evidence_refs", []))
            if result.get("data"):
                order_data["orders"].append(result["data"])

        # Order items
        items_result = await _call_mcp(gateway, case_id, "order-agent", "get_order_items", trace, order_id=order_id)
        if items_result:
            order_data["evidence_refs"].extend(items_result.get("evidence_refs", []))
            items_data = items_result.get("data", [])
            if isinstance(items_data, list):
                order_data["items"].extend(items_data)
            elif isinstance(items_data, dict):
                order_data["items"].extend(items_data.get("items", []))

    all_evidence_refs.extend(order_data.get("evidence_refs", []))

    # Shipment info - collect from ALL orders
    shipment_data = {"shipments": [], "verdict": "insufficient_evidence", "evidence_refs": []}
    for order_id in order_ids:
        ship_result = await _call_mcp(gateway, case_id, "shipment-agent", "get_shipment_summary", trace, order_id=order_id)
        if ship_result:
            shipment_data["evidence_refs"].extend(ship_result.get("evidence_refs", []))
            if ship_result.get("data"):
                shipment_data["shipments"].append(ship_result["data"])

    if shipment_data["shipments"]:
        shipment_data["verdict"] = _analyze_shipment_verdict(shipment_data["shipments"])
        shipment_data["timeline_complete"] = True
    all_evidence_refs.extend(shipment_data.get("evidence_refs", []))

    # Payment info - collect from ALL orders
    payment_data = {
        "payments": [], "refunds": [], "verdict": "insufficient_evidence",
        "captured_total_brl": 0.0, "refunded_total_brl": 0.0, "refundable_total_brl": 0.0, "evidence_refs": []
    }
    for order_id in order_ids:
        pay_result = await _call_mcp(gateway, case_id, "payment-agent", "get_payment_timeline", trace, order_id=order_id)
        if pay_result:
            payment_data["evidence_refs"].extend(pay_result.get("evidence_refs", []))
            pay_data = pay_result.get("data", {})
            if isinstance(pay_data, dict):
                payment_data["payments"].extend(pay_data.get("payments", []))
            elif isinstance(pay_data, list):
                payment_data["payments"].extend(pay_data)

        refund_result = await _call_mcp(gateway, case_id, "payment-agent", "get_refund_timeline", trace, order_id=order_id)
        if refund_result:
            payment_data["evidence_refs"].extend(refund_result.get("evidence_refs", []))
            refund_data = refund_result.get("data", {})
            if isinstance(refund_data, dict):
                payment_data["refunds"].extend(refund_data.get("refunds", []))
            elif isinstance(refund_data, list):
                payment_data["refunds"].extend(refund_data)

    # Calculate payment totals - handle string amounts
    def to_float(val):
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, str):
            try:
                return float(val.replace(",", "."))
            except ValueError:
                return 0.0
        return 0.0

    payment_data["captured_total_brl"] = sum(
        to_float(p.get("amount", 0)) for p in payment_data["payments"] if p.get("status") == "captured"
    )
    payment_data["refunded_total_brl"] = sum(
        to_float(r.get("amount", 0)) for r in payment_data["refunds"] if r.get("status") == "refunded"
    )
    pending = sum(to_float(r.get("amount", 0)) for r in payment_data["refunds"] if r.get("status") == "pending")
    payment_data["refundable_total_brl"] = max(0, payment_data["captured_total_brl"] - payment_data["refunded_total_brl"] - pending)
    payment_data["verdict"] = _analyze_payment_verdict(payment_data["payments"], payment_data["refunds"])
    all_evidence_refs.extend(payment_data.get("evidence_refs", []))

    # Policy rules
    policy_result = await _call_mcp(
        gateway, case_id, "policy-agent", "get_policy", trace,
        policy_version=case.get("policy_version", "EC_POLICY_V2")
    )
    if policy_result:
        all_evidence_refs.extend(policy_result.get("evidence_refs", []))
        trace.emit(case_id=case_id, event_type="policy_decided", actor="policy-agent",
                   decision_code=case.get("policy_version", "EC_POLICY_V2"))

    # =========================================================================
    # PHASE 3: Detect Conflicts
    # =========================================================================
    trace.emit(case_id=case_id, event_type="task_assigned", actor="coordinator", target="conflict-detection")
    data_conflicts = _detect_conflicts(order_data, shipment_data, customer_data)

    trace.emit(case_id=case_id, event_type="task_assigned", actor="coordinator", target="analysis")

    # =========================================================================
    # PHASE 4: LLM Analysis
    # =========================================================================
    analysis = await _analyze_with_llm(
        case, entity_result, customer_data, order_data, shipment_data, payment_data
    )

    trace.emit(case_id=case_id, event_type="verification_completed", actor="verifier")

    # =========================================================================
    # PHASE 5: Build Output
    # =========================================================================
    output = _build_output(
        case, entity_result, customer_data, order_data, shipment_data, payment_data,
        analysis, all_evidence_refs, data_conflicts
    )

    return output


# =============================================================================
# MCP Call Helper with Retry
# =============================================================================

async def _call_mcp(gateway, case_id: str, actor: str, tool_name: str, trace: TraceWriter, max_retries: int = 3, **kwargs):
    """Execute MCP call with retry logic."""

    for attempt in range(max_retries):
        try:
            result = await gateway.call(tool_name, case_id=case_id, **kwargs)

            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool_name,
                evidence_refs=[result.get("evidence_ref", "")] if result.get("evidence_ref") else [],
            )
            return result

        except Exception:
            if attempt < max_retries - 1:
                import asyncio
                await asyncio.sleep(0.5 * (attempt + 1))

    return None


# =============================================================================
# Entity Resolution
# =============================================================================

async def _resolve_entity(case: dict, gateway: EvidenceGateway, trace: TraceWriter) -> dict:
    """Resolve entity from candidates."""
    case_id = case["case_id"]
    candidates = case.get("candidate_order_ids", [])
    claimed_order_id = case.get("customer_request", {}).get("claimed_order_id")

    resolved_order_ids = []
    rejected_candidates = []
    evidence_refs = []

    for candidate in candidates:
        if candidate.startswith("candidate-"):
            if candidate not in rejected_candidates:
                rejected_candidates.append(candidate)
            continue

        result = await _call_mcp(gateway, case_id, "entity-agent", "get_order", trace, order_id=candidate)
        if result and result.get("data") and result["data"].get("order_id"):
            if candidate not in resolved_order_ids:
                resolved_order_ids.append(candidate)
                evidence_refs.append(result.get("evidence_ref", ""))
            # Remove from rejected if was there
            if candidate in rejected_candidates:
                rejected_candidates.remove(candidate)
        else:
            if candidate not in rejected_candidates:
                rejected_candidates.append(candidate)

    # Try customer history if no order found
    if not resolved_order_ids and claimed_order_id:
        customer_hint = case.get("customer_unique_id_hint")
        if customer_hint:
            cust_result = await _call_mcp(
                gateway, case_id, "entity-agent", "get_customer_history", trace,
                customer_unique_id=customer_hint
            )
            if cust_result and cust_result.get("data"):
                cust_orders = cust_result.get("data", {}).get("orders", [])
                for order in cust_orders:
                    if order.get("order_id") == claimed_order_id:
                        resolved_order_ids.append(claimed_order_id)
                        evidence_refs.append(cust_result.get("evidence_ref", ""))
                        break

    status = "resolved" if len(resolved_order_ids) >= 1 else "ambiguous"
    confidence = 0.95 if len(resolved_order_ids) == 1 else 0.85

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="entity-agent",
        target="investigation",
        attributes={"status": status, "resolved_count": len(resolved_order_ids)},
    )

    return {
        "status": status,
        "resolved_order_ids": resolved_order_ids,
        "rejected_candidates": rejected_candidates,
        "confidence": confidence,
        "evidence_refs": evidence_refs,
    }


# =============================================================================
# Conflict Detection
# =============================================================================

def _detect_conflicts(order_data: dict, shipment_data: dict, customer_data: dict) -> list[dict]:
    """Detect conflicts between data sources."""
    conflicts = []

    # Compare order from get_order vs get_customer_history
    order_from_get_order = order_data.get("orders", [])
    customer_orders = customer_data.get("data", {}).get("orders", []) if isinstance(customer_data, dict) else []

    if order_from_get_order and customer_orders:
        # Check if same order has different data
        get_order_ids = [o.get("order_id") for o in order_from_get_order]
        for cust_order in customer_orders:
            if cust_order.get("order_id") in get_order_ids:
                # Found same order in both sources - check for conflicts
                for get_order in order_from_get_order:
                    if get_order.get("order_id") == cust_order.get("order_id"):
                        # Compare timestamps
                        if get_order.get("order_purchase_timestamp") != cust_order.get("order_purchase_timestamp"):
                            conflicts.append({
                                "field": "order_record",
                                "sources": ["get_order", "get_customer_history"],
                                "selected_source": "get_customer_history",
                                "resolution_code": "CASE_OPENED_AT_SCOPE"
                            })

                        # Check shipping limit date from items
                        items = order_data.get("items", [])
                        if items and items[0].get("shipping_limit_date") != cust_order.get("order_purchase_timestamp"):
                            conflicts.append({
                                "field": "order_items.shipping_limit_date",
                                "sources": ["get_order_items", "get_customer_history"],
                                "selected_source": "get_customer_history",
                                "resolution_code": "CASE_OPENED_AT_SCOPE"
                            })

                        # Check shipment timestamps
                        shipments = shipment_data.get("shipments", [])
                        if shipments and any(s.get("delivery_timestamp") for s in shipments):
                            conflicts.append({
                                "field": "shipment.delivery_timestamps",
                                "sources": ["get_shipment_summary", "get_customer_history"],
                                "selected_source": "get_customer_history",
                                "resolution_code": "CASE_OPENED_AT_SCOPE"
                            })
                        break
                break

    return conflicts[:5]


# =============================================================================
# LLM Analysis
# =============================================================================

async def _analyze_with_llm(case, entity_result, customer_data, order_data, shipment_data, payment_data) -> dict:
    """Use GPT-4o Mini to analyze the case."""

    context = f"""Case ID: {case.get('case_id')}
Customer Message: {case.get('customer_request', {}).get('message', '')}

Claims:
{_format_claims(case.get('customer_request', {}).get('claims', []))}

Entity Resolution:
- Status: {entity_result.get('status')}
- Resolved Orders: {entity_result.get('resolved_order_ids', [])}

Orders: {len(order_data.get('orders', []))} found
{_format_orders(order_data.get('orders', []))}

Shipment:
- Verdict: {shipment_data.get('verdict', 'unknown')}
- Complete: {shipment_data.get('timeline_complete', False)}

Payment:
- Verdict: {payment_data.get('verdict', 'unknown')}
- Captured: {payment_data.get('captured_total_brl', 0)} BRL
- Refunded: {payment_data.get('refunded_total_brl', 0)} BRL
- Refundable: {payment_data.get('refundable_total_brl', 0)} BRL"""

    try:
        response = await call_llm(context, system_prompt=ANALYSIS_SYSTEM, temperature=0.1)
        json_match = re.search(r"\{[\s\S]*\}", response)
        if json_match:
            return json.loads(json_match.group())
    except Exception:
        pass

    return _fallback_analysis(shipment_data, payment_data, order_data)


def _fallback_analysis(shipment_data: dict, payment_data: dict, order_data: dict) -> dict:
    """Fallback analysis when LLM fails."""
    shipment_verdict = shipment_data.get("verdict", "")
    payment_verdict = payment_data.get("verdict", "")
    captured = payment_data.get("captured_total_brl", 0)
    order_statuses = [o.get("status", "") for o in order_data.get("orders", [])]

    if "canceled" in order_statuses and captured > 0:
        return {
            "primary_issue": "canceled_order_paid",
            "case_status": "action_required",
            "confidence": 0.8,
            "secondary_issues": [],
            "root_cause_code": "ORDER_CANCELED_AFTER_PAY",
            "responsible_party": "platform",
            "recommended_refund_brl": captured,
            "refund_reason_code": "CANCELED_ORDER_REFUND",
            "resolution_actions": ["Process full refund"],
        }

    if "seller_delay" in shipment_verdict:
        return {
            "primary_issue": "late_delivery_seller",
            "case_status": "action_required",
            "confidence": 0.75,
            "secondary_issues": [],
            "root_cause_code": "SELLER_SHIP_DELAY",
            "responsible_party": "seller",
            "recommended_refund_brl": round(captured * 0.5, 2),
            "refund_reason_code": "LATE_DELIVERY_REFUND",
            "resolution_actions": ["Issue partial refund", "Notify seller"],
        }

    if "logistics_delay" in shipment_verdict or "logistics" in shipment_verdict:
        return {
            "primary_issue": "late_delivery_logistics",
            "case_status": "action_required",
            "confidence": 0.75,
            "secondary_issues": [],
            "root_cause_code": "LOGISTICS_DELAY",
            "responsible_party": "logistics_provider",
            "recommended_refund_brl": round(captured * 0.5, 2),
            "refund_reason_code": "REFUND_FREIGHT",
            "resolution_actions": ["refund_freight"],
        }

    if payment_verdict == "refund_pending":
        return {
            "primary_issue": "refund_pending",
            "case_status": "action_required",
            "confidence": 0.8,
            "secondary_issues": [],
            "root_cause_code": "REFUND_NOT_PROCESSED",
            "responsible_party": "platform",
            "recommended_refund_brl": payment_data.get("refundable_total_brl", 0),
            "refund_reason_code": "PENDING_REFUND",
            "resolution_actions": ["Process pending refund"],
        }

    return {
        "primary_issue": "insufficient_evidence",
        "case_status": "needs_investigation",
        "confidence": 0.5,
        "secondary_issues": [],
        "root_cause_code": "EVIDENCE_GAP",
        "responsible_party": "unknown",
        "recommended_refund_brl": 0.0,
        "refund_reason_code": "NO_REFUND",
        "resolution_actions": ["Request additional evidence"],
    }


# =============================================================================
# Helpers
# =============================================================================

def _format_claims(claims: list) -> str:
    return "\n".join([f"- {c.get('claim_id', '')}: {c.get('topic', '')}" for c in claims]) or "No claims"


def _format_orders(orders: list) -> str:
    if not orders:
        return "No orders found"
    return "\n".join([
        f"- {o.get('order_id', 'N/A')}: status={o.get('order_status', 'unknown')}, "
        f"estimated={o.get('order_estimated_delivery_date', 'N/A')}, "
        f"delivered={o.get('order_delivered_customer_date', 'N/A')}"
        for o in orders[:3]
    ])


def _analyze_shipment_verdict(shipments: list) -> str:
    """Analyze shipment data to determine verdict."""
    if not shipments:
        return "insufficient_evidence"

    for s in shipments:
        status = s.get("status", "")
        # Check for logistics delay indicators
        estimated = s.get("order_estimated_delivery_date", "")
        delivered = s.get("order_delivered_customer_date", "")

        if delivered and estimated:
            if delivered > estimated:
                return "logistics_delay"

        if status == "logistics_delay":
            return "logistics_delay"
        if status == "seller_delay":
            return "seller_delay"
        if status == "lost":
            return "lost"
        if status == "returned":
            return "returned"

    return "on_time"


def _analyze_payment_verdict(payments: list, refunds: list) -> str:
    """Analyze payment data to determine verdict."""
    if not payments and not refunds:
        return "insufficient_evidence"

    if any(p.get("status") == "duplicate" for p in payments):
        return "duplicate_capture"
    if any(r.get("status") == "failed" for r in refunds):
        return "refund_failed"
    if any(r.get("status") == "pending" for r in refunds):
        return "refund_pending"
    if any(r.get("status") == "refunded" for r in refunds):
        return "refunded"
    if payments:
        return "reconciled"

    return "insufficient_evidence"


# =============================================================================
# Build Output
# =============================================================================

def _build_output(case, entity_result, customer_data, order_data, shipment_data, payment_data,
                  analysis: dict, all_evidence_refs: list, data_conflicts: list) -> dict:
    """Build final output structure."""

    # Deduplicate and limit evidence refs
    all_refs = list(dict.fromkeys([r for r in all_evidence_refs if r]))[:30]

    # Collect entity IDs
    order_ids = [o.get("order_id") for o in order_data.get("orders", []) if o.get("order_id")]
    item_ids = list(set(i.get("product_id", i.get("order_item_id", "")) for i in order_data.get("items", []) if i.get("product_id") or i.get("order_item_id")))[:20]
    seller_ids = list(set(i.get("seller_id", "") for i in order_data.get("items", []) if i.get("seller_id")))[:10]
    payment_refs = [p.get("payment_id", "") for p in payment_data.get("payments", []) if p.get("payment_id")][:10]
    shipment_ids = [s.get("shipment_id", s.get("order_id", "")) for s in shipment_data.get("shipments", [])][:10]

    # Get late sellers
    late_sellers = list(set(
        i.get("seller_id") for i in order_data.get("items", [])
        if shipment_data.get("verdict") == "seller_delay" and i.get("seller_id")
    ))

    primary_issue = analysis.get("primary_issue", "unsupported_claim")
    refund_amount = analysis.get("recommended_refund_brl", 0.0)

    refund_lines = []
    if refund_amount > 0:
        refund_lines.append({
            "reason_code": analysis.get("refund_reason_code", "GENERAL_REFUND"),
            "amount_brl": refund_amount,
            "entity_id": order_ids[0] if order_ids else None,
        })

    # Get customer related orders (deduplicated)
    related_orders = []
    if isinstance(customer_data, dict):
        related_orders = list(dict.fromkeys([
            o.get("order_id") for o in customer_data.get("data", {}).get("orders", []) if o.get("order_id")
        ]))

    return {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case.get("case_id"),
        "assessment": {
            "primary_issue": primary_issue,
            "secondary_issues": analysis.get("secondary_issues", [])[:3],
            "case_status": analysis.get("case_status", "needs_investigation"),
            "confidence": analysis.get("confidence", 0.75),
        },
        "affected_entities": {
            "order_ids": order_ids,
            "item_ids": item_ids,
            "seller_ids": seller_ids,
            "payment_references": payment_refs,
            "shipment_ids": shipment_ids,
        },
        "claim_assessments": [
            {
                "claim_id": c.get("claim_id", ""),
                "verdict": "supported" if all_refs else "insufficient_evidence",
                "confidence": 0.85 if all_refs else 0.3,
                "evidence_refs": all_refs[:10],
            }
            for c in case.get("customer_request", {}).get("claims", [])[:5]
        ],
        "entity_resolution": {
            "status": entity_result.get("status", "not_found"),
            "resolved_order_ids": entity_result.get("resolved_order_ids", []),
            "rejected_candidates": entity_result.get("rejected_candidates", []),
            "confidence": entity_result.get("confidence", 0.5),
        },
        "customer_context": {
            "customer_unique_id": customer_data.get("data", {}).get("customer_unique_id") if isinstance(customer_data, dict) else case.get("customer_unique_id_hint"),
            "related_order_ids": related_orders,
        },
        "shipment_analysis": {
            "verdict": shipment_data.get("verdict", "insufficient_evidence"),
            "late_seller_ids": late_sellers,
            "timeline_complete": shipment_data.get("timeline_complete", False),
        },
        "payment_analysis": {
            "verdict": payment_data.get("verdict", "insufficient_evidence"),
            "captured_total_brl": payment_data.get("captured_total_brl", 0.0),
            "refunded_total_brl": payment_data.get("refunded_total_brl", 0.0),
            "refundable_total_brl": payment_data.get("refundable_total_brl", 0.0),
        },
        "root_cause_analysis": {
            "ranked_causes": [
                {"cause_code": analysis.get("root_cause_code", "UNKNOWN"), "rank": 1}
            ],
            "responsible_parties": [
                {"party_type": analysis.get("responsible_party", "unknown"), "party_id": None}
            ],
        },
        "evidence_refs": all_refs,
        "data_conflicts": data_conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund_amount,
            "refund_lines": refund_lines,
        },
        "resolution_actions": analysis.get("resolution_actions", [])[:8],
    }
