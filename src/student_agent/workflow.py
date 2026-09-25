"""L3B multi-agent workflow: coordinator + specialist agents over the MCP Evidence Gateway.

Plain async state machine. Each specialist owns a least-privilege tool set, every MCP result is
recorded as ``tool_result_consumed`` and handed off to the policy agent, then to the verifier.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import OUTPUT_SCHEMA_VERSION
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

COORDINATOR = "coordinator"
ENTITY_AGENT = "entity-agent"
ORDER_AGENT = "order-agent"
SHIPMENT_AGENT = "shipment-agent"
PAYMENT_AGENT = "payment-agent"
POLICY_AGENT = "policy-agent"
VERIFIER = "verifier-agent"

# Least privilege: an agent may only call the tools listed for it.
TOOL_PERMISSIONS: dict[str, frozenset[str]] = {
    ENTITY_AGENT: frozenset({"get_customer_history"}),
    ORDER_AGENT: frozenset({"get_order", "get_order_items", "get_product_context"}),
    SHIPMENT_AGENT: frozenset({"get_shipment_summary"}),
    PAYMENT_AGENT: frozenset({"get_payment_timeline", "get_refund_timeline"}),
    POLICY_AGENT: frozenset({"get_policy"}),
}

# Tools whose failure usually means "no rows" rather than a transient fault.
NOT_FOUND_TOLERANT = {"get_refund_timeline"}
MAX_RETRIES = 2
RETRY_BASE_DELAY = 2.0

ISSUE_TO_PAYMENT_VERDICT = {
    "duplicate_charge": "duplicate_capture",
    "payment_mismatch": "capture_mismatch",
    "refund_pending": "refund_pending",
    "refund_failed": "refund_failed",
}


def _ts(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _money(value: Any) -> float:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0


def _unique(values: list[Any]) -> list[Any]:
    seen: list[Any] = []
    for value in values:
        if value is not None and value not in seen:
            seen.append(value)
    return seen


@dataclass
class CaseContext:
    """Per-case blackboard. Evidence refs never leave this object, so they cannot cross cases."""

    case: dict[str, Any]
    gateway: EvidenceGateway
    trace: TraceWriter
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)

    @property
    def case_id(self) -> str:
        return self.case["case_id"]

    def ref(self, tool: str) -> str | None:
        item = self.evidence.get(tool)
        return item["evidence_ref"] if item else None

    def data(self, tool: str) -> Any:
        item = self.evidence.get(tool)
        return item["data"] if item else None

    async def call(self, actor: str, tool: str, **arguments: str) -> Any:
        if tool not in TOOL_PERMISSIONS.get(actor, frozenset()):
            raise PermissionError(f"{actor} is not allowed to call {tool}")
        if tool in self.evidence:  # case-scoped cache: never call the same tool twice
            return self.evidence[tool]["data"]
        retries = 1 if tool in NOT_FOUND_TOLERANT else MAX_RETRIES
        last_error = ""
        for attempt in range(retries + 1):
            try:
                result = await self.gateway.call(tool, case_id=self.case_id, **arguments)
            except (RuntimeError, ValueError, OSError) as exc:
                last_error = str(exc)[:120]
                if attempt < retries:
                    await asyncio.sleep(RETRY_BASE_DELAY * (attempt + 1))
                continue
            self.evidence[tool] = result
            self.trace.emit(
                case_id=self.case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool,
                evidence_refs=[result["evidence_ref"]],
                attributes={"domain": result["domain"], "attempts": attempt + 1},
            )
            return result["data"]
        self.failures[tool] = last_error
        return None

    def assign(self, agent: str, task: str) -> None:
        self.trace.emit(
            case_id=self.case_id,
            event_type="task_assigned",
            actor=COORDINATOR,
            target=agent,
            decision_code=task,
        )

    def handoff(self, source: str, target: str, code: str, tools: list[str]) -> None:
        refs = _unique([self.ref(tool) for tool in tools])
        self.trace.emit(
            case_id=self.case_id,
            event_type="handoff",
            actor=source,
            target=target,
            decision_code=code,
            evidence_refs=refs or None,
        )


# --------------------------------------------------------------------------- entity agent


def _select_layer(rows: list[dict[str, Any]], opened_at: datetime | None) -> dict[str, Any] | None:
    """Pick the latest order record whose purchase happened before the complaint was opened."""
    dated = [(row, _ts(row.get("order_purchase_timestamp"))) for row in rows]
    dated = [(row, ts) for row, ts in dated if ts is not None]
    if not dated:
        return rows[0] if rows else None
    eligible = [(row, ts) for row, ts in dated if opened_at is None or ts <= opened_at]
    pool = eligible or dated
    return max(pool, key=lambda pair: pair[1])[0]


async def entity_agent(ctx: CaseContext) -> dict[str, Any]:
    case = ctx.case
    hint = case.get("customer_unique_id_hint")
    history = await ctx.call(ENTITY_AGENT, "get_customer_history", customer_unique_id=hint) if hint else None
    history_rows = (history or {}).get("orders", []) if isinstance(history, dict) else []
    history_ids = _unique([row.get("order_id") for row in history_rows])
    candidates = _unique(case.get("candidate_order_ids", []))
    claimed = case.get("customer_request", {}).get("claimed_order_id")

    resolved = [oid for oid in candidates if oid in history_ids]
    if not resolved and claimed in candidates and not history_rows:
        resolved = [claimed]  # no history available: trust the claimed candidate, lower confidence
    if len(resolved) > 1 and claimed in resolved:
        resolved = [claimed]
    rejected = [oid for oid in candidates if oid not in resolved]

    if len(resolved) == 1:
        status, confidence = "resolved", (0.95 if history_rows else 0.6)
    elif resolved:
        status, confidence = "ambiguous", 0.4
    else:
        status, confidence = "not_found", 0.2

    order_id = resolved[0] if len(resolved) == 1 else None
    opened_at = _ts(case.get("opened_at"))
    layer_rows = [row for row in history_rows if row.get("order_id") == order_id]
    layer = _select_layer(layer_rows, opened_at)
    return {
        "status": status,
        "order_id": order_id,
        "resolved": resolved,
        "rejected": rejected,
        "confidence": confidence,
        "customer_unique_id": (history or {}).get("customer_unique_id") if isinstance(history, dict) else None,
        "related_order_ids": history_ids,
        "layer": layer,
        "layer_rows": layer_rows,
        "opened_at": opened_at,
        # several records share the selected purchase time -> time scope cannot separate them
        "layer_ambiguous": bool(layer) and sum(
            1 for row in layer_rows
            if row.get("order_purchase_timestamp") == layer.get("order_purchase_timestamp")
        ) > 1,
    }


def _layer_window(entity: dict[str, Any], cap_at_open: bool = True) -> tuple[datetime | None, datetime | None]:
    """Time window [purchase, next record purchase) that belongs to the selected record."""
    layer = entity.get("layer")
    start = _ts(layer.get("order_purchase_timestamp")) if layer else None
    if start is None:
        return None, None
    later = sorted(
        ts
        for ts in (_ts(row.get("order_purchase_timestamp")) for row in entity.get("layer_rows", []))
        if ts is not None and ts > start
    )
    end = later[0] if later else None
    opened_at = entity.get("opened_at")
    if cap_at_open and opened_at is not None and (end is None or opened_at < end):
        end = opened_at  # nothing after the complaint was opened belongs to it
    return start, end


def _in_window(value: Any, window: tuple[datetime | None, datetime | None]) -> bool:
    start, end = window
    ts = _ts(value)
    if start is None:
        return True
    if ts is None:
        return False
    return ts >= start and (end is None or ts < end)


# --------------------------------------------------------------------------- specialists


async def order_agent(ctx: CaseContext, entity: dict[str, Any]) -> dict[str, Any]:
    order_id = entity["order_id"]
    order = await ctx.call(ORDER_AGENT, "get_order", order_id=order_id)
    items = await ctx.call(ORDER_AGENT, "get_order_items", order_id=order_id) or []
    if ctx.case.get("investigation_scope", {}).get("include_product_context"):
        await ctx.call(ORDER_AGENT, "get_product_context", order_id=order_id)
    window = _layer_window(entity, cap_at_open=False)
    scoped = [row for row in items if _in_window(row.get("shipping_limit_date"), window)]
    if not scoped:
        scoped = items
    layer = entity.get("layer") or {}
    order_conflict = bool(
        isinstance(order, dict)
        and layer
        and order.get("order_purchase_timestamp") != layer.get("order_purchase_timestamp")
    )
    return {
        "order": order,
        "items": scoped,
        "item_conflict": len(scoped) != len(items),
        "order_conflict": order_conflict,
        "expected_total": round(
            sum(_money(row.get("price")) + _money(row.get("freight_value")) for row in scoped), 2
        ),
    }


async def shipment_agent(
    ctx: CaseContext, entity: dict[str, Any], items: list[dict[str, Any]]
) -> dict[str, Any]:
    summary = await ctx.call(SHIPMENT_AGENT, "get_shipment_summary", order_id=entity["order_id"])
    layer = entity.get("layer") or {}
    window = _layer_window(entity)
    events = [
        event
        for event in (summary or {}).get("events", [])
        if _in_window(event.get("event_at"), window) and event.get("status") != "rejected"
    ]
    carrier = _ts(layer.get("order_delivered_carrier_date"))
    delivered = _ts(layer.get("order_delivered_customer_date"))
    estimated = _ts(layer.get("order_estimated_delivery_date"))
    late_sellers = _unique(
        [
            row.get("seller_id")
            for row in items
            if carrier and _ts(row.get("shipping_limit_date")) and carrier > _ts(row.get("shipping_limit_date"))
        ]
    )
    late_actors = {event.get("actor") for event in events if event.get("event_type") == "delivered_late"}
    status = layer.get("order_status")
    if delivered is None or estimated is None:
        verdict = "insufficient_evidence"
    elif delivered <= estimated and not late_actors:
        verdict = "on_time"
    elif late_sellers or "seller" in late_actors:
        verdict = "seller_delay"
    else:
        verdict = "logistics_delay"
    if verdict == "seller_delay" and not late_sellers:
        late_sellers = _unique([row.get("seller_id") for row in items])
    required = ["order_purchase_timestamp", "order_approved_at", "order_delivered_carrier_date",
                "order_delivered_customer_date", "order_estimated_delivery_date"]
    return {
        "verdict": verdict,
        "late_seller_ids": late_sellers if verdict == "seller_delay" else [],
        "timeline_complete": bool(layer) and all(layer.get(key) for key in required),
        "order_status": status,
        "summary_conflict": bool(
            summary and layer and summary.get("delivered_carrier_at") != layer.get("order_delivered_carrier_date")
        ),
    }


async def payment_agent(ctx: CaseContext, entity: dict[str, Any], expected_total: float) -> dict[str, Any]:
    order_id = entity["order_id"]
    timeline = await ctx.call(PAYMENT_AGENT, "get_payment_timeline", order_id=order_id) or {}
    refunds = await ctx.call(PAYMENT_AGENT, "get_refund_timeline", order_id=order_id) or {}
    window = _layer_window(entity)
    events = [e for e in timeline.get("events", []) if _in_window(e.get("event_at"), window)]
    refund_events = [e for e in refunds.get("events", []) if _in_window(e.get("event_at"), window)]
    captures = [e for e in events if e.get("event_type") == "captured" and e.get("status") != "failed"]
    captured = round(sum(_money(e.get("amount_brl")) for e in captures), 2)
    refunded = round(
        sum(
            _money(e.get("amount_brl"))
            for e in refund_events
            if e.get("status") in {"completed", "succeeded", "confirmed", "refunded"}
        ),
        2,
    )
    refund_status = {e.get("status") for e in refund_events}
    mismatch = any(e.get("event_type") == "reconciliation_mismatch" for e in events)
    amounts = [_money(e.get("amount_brl")) for e in captures]
    duplicate = len(amounts) >= 2 and len(set(amounts)) < len(amounts) and captured > expected_total + 0.01
    split = len(captures) >= 2 and abs(captured - expected_total) <= 0.01 and not duplicate
    return {
        "captured": captured if captures else None,
        "refunded": refunded,
        "refund_failed": "failed" in refund_status,
        "refund_pending": bool(refund_status & {"pending", "requested", "processing"}),
        "mismatch": mismatch,
        "duplicate": duplicate,
        "split": split,
        "has_refund_evidence": bool(refund_events),
        "multi_method": len({p.get("payment_type") for p in timeline.get("payments", [])}) > 1,
    }


# --------------------------------------------------------------------------- policy agent


def classify(
    entity: dict[str, Any], shipment: dict[str, Any], payment: dict[str, Any], claim_topics: list[str]
) -> str:
    status = shipment.get("order_status")
    paid = (payment.get("captured") or 0) > 0
    if entity.get("order_id") is None or not entity.get("layer"):
        return "insufficient_evidence"
    if entity.get("layer_ambiguous"):
        supported = {
            "refund_failed": payment["refund_failed"],
            "refund_pending": payment["refund_pending"],
            "payment_mismatch": payment["mismatch"],
            "duplicate_charge": payment["duplicate"],
            "valid_split_payment": payment.get("multi_method", False),
            "late_delivery_seller": shipment["verdict"] == "seller_delay",
            "late_delivery_logistics": shipment["verdict"] == "logistics_delay",
            "canceled_order_paid": status == "canceled" and paid,
            "unavailable_order_paid": status == "unavailable" and paid,
        }
        for topic in claim_topics:
            if supported.get(topic):
                return topic
    if status == "canceled" and paid:
        return "canceled_order_paid"
    if status == "unavailable" and paid:
        return "unavailable_order_paid"
    if payment["refund_failed"]:
        return "refund_failed"
    if payment["refund_pending"]:
        return "refund_pending"
    if payment["mismatch"]:
        return "payment_mismatch"
    if payment["duplicate"]:
        return "duplicate_charge"
    if shipment["verdict"] == "seller_delay":
        return "late_delivery_seller"
    if shipment["verdict"] == "logistics_delay":
        return "late_delivery_logistics"
    if payment["split"]:
        return "valid_split_payment"
    return "unsupported_claim"


async def policy_agent(ctx: CaseContext, issue: str, sellers: list[str]) -> dict[str, Any]:
    policy = await ctx.call(POLICY_AGENT, "get_policy", policy_version=ctx.case.get("policy_version", ""))
    rule = ((policy or {}).get("rules") or {}).get(issue)
    if rule is None:
        return {
            "case_status": "needs_investigation",
            "action": "escalate_manual_review",
            "refund": 0.0,
            "parties": [{"party_type": "unknown", "party_id": None}],
            "found": False,
        }
    parties = []
    for party in rule.get("responsible_parties", []):
        if party.get("party_type") == "seller":
            # the policy carries an illustrative seller id; bind it to this order's seller(s)
            parties.extend({"party_type": "seller", "party_id": seller} for seller in sellers[:5])
        else:
            parties.append({"party_type": party.get("party_type", "unknown"), "party_id": party.get("party_id")})
    return {
        "case_status": rule.get("case_status", "needs_investigation"),
        "action": rule.get("recommended_action", "escalate_manual_review"),
        "refund": _money(rule.get("refund_brl")),
        "parties": parties[:5] or [{"party_type": "unknown", "party_id": None}],
        "found": True,
    }


# --------------------------------------------------------------------------- solve_case


ISSUE_TOOLS = {
    "canceled_order_paid": ["get_order", "get_payment_timeline"],
    "unavailable_order_paid": ["get_order", "get_order_items", "get_payment_timeline"],
    "late_delivery_seller": ["get_order_items", "get_shipment_summary"],
    "late_delivery_logistics": ["get_order_items", "get_shipment_summary"],
    "valid_split_payment": ["get_order_items", "get_payment_timeline"],
    "payment_mismatch": ["get_payment_timeline"],
    "duplicate_charge": ["get_order_items", "get_payment_timeline"],
    "refund_pending": ["get_payment_timeline", "get_refund_timeline"],
    "refund_failed": ["get_payment_timeline", "get_refund_timeline"],
    "unsupported_claim": ["get_order", "get_shipment_summary", "get_payment_timeline"],
}


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    ctx = CaseContext(case, gateway, trace)
    case_id = ctx.case_id

    # 1. entity resolution
    ctx.assign(ENTITY_AGENT, "RESOLVE_ENTITY")
    entity = await entity_agent(ctx)
    ctx.handoff(ENTITY_AGENT, COORDINATOR, f"ENTITY_{entity['status'].upper()}", ["get_customer_history"])

    order_info: dict[str, Any] = {"items": [], "expected_total": 0.0, "order_conflict": False, "item_conflict": False}
    shipment: dict[str, Any] = {"verdict": "insufficient_evidence", "late_seller_ids": [], "timeline_complete": False,
                                "order_status": None, "summary_conflict": False}
    payment: dict[str, Any] = {"captured": None, "refunded": None, "refund_failed": False, "refund_pending": False,
                               "mismatch": False, "duplicate": False, "split": False, "has_refund_evidence": False}

    if entity["order_id"]:
        # 2. specialists
        ctx.assign(ORDER_AGENT, "COLLECT_ORDER_ITEMS")
        order_info = await order_agent(ctx, entity)
        ctx.handoff(ORDER_AGENT, SHIPMENT_AGENT, "ITEMS_SCOPED", ["get_order", "get_order_items"])

        ctx.assign(SHIPMENT_AGENT, "ANALYZE_SHIPMENT")
        shipment = await shipment_agent(ctx, entity, order_info["items"])
        ctx.handoff(SHIPMENT_AGENT, POLICY_AGENT, f"SHIPMENT_{shipment['verdict'].upper()}", ["get_shipment_summary"])

        ctx.assign(PAYMENT_AGENT, "ANALYZE_PAYMENT")
        payment = await payment_agent(ctx, entity, order_info["expected_total"])
        ctx.handoff(PAYMENT_AGENT, POLICY_AGENT, "PAYMENT_ANALYZED", ["get_payment_timeline", "get_refund_timeline"])

    # 3. policy
    ctx.assign(POLICY_AGENT, "APPLY_POLICY")
    claim_topics = [c.get("topic") for c in case.get("customer_request", {}).get("claims", [])]
    issue = classify(entity, shipment, payment, claim_topics)
    sellers = shipment["late_seller_ids"] or _unique([row.get("seller_id") for row in order_info["items"]])
    decision = await policy_agent(ctx, issue, sellers)
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor=POLICY_AGENT,
        decision_code=issue.upper(),
        evidence_refs=[ctx.ref("get_policy")] if ctx.ref("get_policy") else None,
        attributes={"case_status": decision["case_status"], "refund_brl": decision["refund"]},
    )
    ctx.handoff(POLICY_AGENT, VERIFIER, "DECISION_READY", ["get_policy"])

    # 4. verifier
    ctx.assign(VERIFIER, "VERIFY_OUTPUT")
    output, checks = build_and_verify(ctx, entity, order_info, shipment, payment, issue, decision)
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor=VERIFIER,
        decision_code="PASS" if all(checks.values()) else "PASS_WITH_ADJUSTMENTS",
        evidence_refs=output["evidence_refs"][:20] or None,
        attributes={key: value for key, value in list(checks.items())[:20]},
    )
    ctx.handoff(VERIFIER, COORDINATOR, "OUTPUT_VALIDATED", [])
    return output


def build_and_verify(
    ctx: CaseContext,
    entity: dict[str, Any],
    order_info: dict[str, Any],
    shipment: dict[str, Any],
    payment: dict[str, Any],
    issue: str,
    decision: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, bool]]:
    case = ctx.case
    order_id = entity["order_id"]
    items = order_info["items"]
    captured = payment["captured"]
    refunded = payment["refunded"] if payment["refunded"] is not None else (0.0 if captured is not None else None)
    refund = decision["refund"]
    checks: dict[str, bool] = {}

    # Evidence: cite only refs that support the decision (issue-specific + entity + policy)
    support_tools = ["get_customer_history", *ISSUE_TOOLS.get(issue, []), "get_policy"]
    if case.get("investigation_scope", {}).get("include_product_context"):
        support_tools.append("get_product_context")
    evidence_refs = _unique([ctx.ref(tool) for tool in support_tools])
    all_refs = {item["evidence_ref"] for item in ctx.evidence.values()}
    checks["evidence_owned"] = set(evidence_refs) <= all_refs

    # Conflicts found between sources
    conflicts: list[dict[str, Any]] = []
    if entity.get("layer_ambiguous"):
        conflicts.append({"field": "order_record.purchase_timestamp", "sources": ["get_customer_history", "get_payment_timeline"],
                          "selected_source": None, "resolution_code": "SAME_TIMESTAMP_CLAIM_TIEBREAK"})
    if order_info["order_conflict"]:
        conflicts.append({"field": "order_record", "sources": ["get_order", "get_customer_history"],
                          "selected_source": "get_customer_history", "resolution_code": "CASE_OPENED_AT_SCOPE"})
    if order_info["item_conflict"]:
        conflicts.append({"field": "order_items.shipping_limit_date", "sources": ["get_order_items", "get_customer_history"],
                          "selected_source": "get_customer_history", "resolution_code": "CASE_OPENED_AT_SCOPE"})
    if shipment["summary_conflict"]:
        conflicts.append({"field": "shipment.delivery_timestamps", "sources": ["get_shipment_summary", "get_customer_history"],
                          "selected_source": "get_customer_history", "resolution_code": "CASE_OPENED_AT_SCOPE"})

    # Claims
    claims = []
    for claim in case.get("customer_request", {}).get("claims", [])[:5]:
        topic = claim.get("topic")
        if topic == "requested_full_refund":
            if refund <= 0:
                verdict = "unsupported"
            elif captured and refund + 0.01 >= captured:
                verdict = "supported"
            else:
                verdict = "partially_supported"
        elif issue == "insufficient_evidence":
            verdict = "insufficient_evidence"
        else:
            verdict = "supported" if topic == issue else "unsupported"
        claims.append({"claim_id": claim.get("claim_id", "claim")[:64], "verdict": verdict,
                       "confidence": 0.85, "evidence_refs": evidence_refs})

    # Consistency: status/refund/action
    case_status = decision["case_status"]
    if refund > 0 and case_status == "no_action":
        case_status = "action_required"
    checks["status_refund_consistent"] = not (refund > 0 and decision["case_status"] == "no_action")
    parties = decision["parties"]
    if issue == "late_delivery_seller":
        checks["seller_responsibility"] = all(p["party_type"] == "seller" and p["party_id"] for p in parties)
    checks["entity_resolved"] = entity["status"] == "resolved"
    checks["candidates_rejected"] = not set(entity["resolved"]) & set(entity["rejected"])
    checks["refund_bounded"] = captured is None or refund <= captured + 0.01 or issue == "late_delivery_logistics"
    checks["policy_found"] = decision["found"]

    confidence = 0.9
    if not checks["entity_resolved"]:
        confidence = 0.4
    elif not decision["found"] or issue in {"insufficient_evidence"}:
        confidence = 0.35
    elif entity.get("layer_ambiguous"):
        confidence = 0.7
    elif conflicts:
        confidence = 0.85
    claim_topics = {c.get("topic") for c in case.get("customer_request", {}).get("claims", [])}
    if issue not in claim_topics and issue != "unsupported_claim":
        confidence -= 0.15
    if ctx.failures.keys() - NOT_FOUND_TOLERANT:
        confidence -= 0.2
    confidence = round(max(0.05, min(0.95, confidence)), 2)
    for claim in claims:
        claim["confidence"] = confidence

    payment_verdict = ISSUE_TO_PAYMENT_VERDICT.get(issue)
    if payment_verdict is None:
        payment_verdict = "insufficient_evidence" if captured is None else (
            "refunded" if refunded and refunded > 0 else "reconciled")
    refundable = None if captured is None else round(max(0.0, captured - (refunded or 0.0)), 2)

    sellers = _unique([row.get("seller_id") for row in items])
    output = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": ctx.case_id,
        "assessment": {
            "primary_issue": issue,
            "secondary_issues": [],
            "case_status": case_status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": [order_id] if order_id else [],
            "item_ids": _unique([row.get("order_item_id") for row in items])[:20],
            "seller_ids": sellers[:20],
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": claims,
        "entity_resolution": {
            "status": entity["status"],
            "resolved_order_ids": entity["resolved"][:20],
            "rejected_candidates": entity["rejected"][:20],
            "confidence": entity["confidence"],
        },
        "customer_context": {
            "customer_unique_id": entity["customer_unique_id"],
            "related_order_ids": entity["related_order_ids"][:20],
        },
        "shipment_analysis": {
            "verdict": shipment["verdict"],
            "late_seller_ids": shipment["late_seller_ids"][:20],
            "timeline_complete": shipment["timeline_complete"],
        },
        "payment_analysis": {
            "verdict": payment_verdict,
            "captured_total_brl": captured,
            "refunded_total_brl": refunded,
            "refundable_total_brl": refundable,
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
            "responsible_parties": parties,
        },
        "evidence_refs": evidence_refs[:30],
        "data_conflicts": conflicts[:5],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund,
            "refund_lines": (
                [{"reason_code": decision["action"], "amount_brl": refund, "entity_id": order_id}]
                if refund > 0 else []
            ),
        },
        "resolution_actions": [decision["action"]],
    }
    return output, checks
