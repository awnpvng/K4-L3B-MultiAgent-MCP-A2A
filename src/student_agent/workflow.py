from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import OUTPUT_SCHEMA_VERSION
from .analysis import (
    MONEY_TOLERANCE,
    ItemFacts,
    OrderFacts,
    PaymentFacts,
    PolicyFacts,
    RefundFacts,
    ShipmentFacts,
    history_order_ids,
    item_facts,
    order_facts,
    payment_facts,
    policy_facts,
    r2,
    refund_facts,
    shipment_facts,
    unique,
)
from .evidence import CaseEvidenceStore, Evidence
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

COORDINATOR = "coordinator"

LATE_ISSUES = {"late_delivery_seller", "late_delivery_logistics"}
NO_ACTION_ISSUES = {"valid_split_payment", "unsupported_claim"}

# Evidence domains that justify each issue; used to keep evidence_refs precise.
ISSUE_DOMAINS: dict[str, set[str]] = {
    "late_delivery_seller": {"shipment", "item", "seller"},
    "late_delivery_logistics": {"shipment"},
    "canceled_order_paid": {"payment", "refund"},
    "unavailable_order_paid": {"payment", "refund"},
    "valid_split_payment": {"payment"},
    "payment_mismatch": {"payment"},
    "duplicate_charge": {"payment"},
    "refund_pending": {"refund", "payment"},
    "refund_failed": {"refund", "payment"},
    "requested_full_refund": {"payment", "refund"},
    "unsupported_claim": set(),
    "insufficient_evidence": set(),
}
BASE_DOMAINS = {"order", "customer", "policy"}

# Default source precedence when the policy does not publish one: lifecycle timelines are
# authoritative over summaries, and the order row is authoritative over derived views.
DEFAULT_PRECEDENCE = [
    "get_payment_timeline",
    "get_refund_timeline",
    "get_order",
    "get_shipment_summary",
    "get_order_items",
    "get_order_payments",
]


@dataclass(frozen=True)
class A2AMessage:
    """Envelope exchanged between agents; correlated by case_id, never carries free text."""

    case_id: str
    sender: str
    recipient: str
    intent: str
    evidence_refs: tuple[str, ...] = ()


@dataclass
class CaseContext:
    case: dict[str, Any]
    store: CaseEvidenceStore
    trace: TraceWriter
    evidence: dict[str, Evidence] = field(default_factory=dict)

    @property
    def case_id(self) -> str:
        return self.case["case_id"]

    def assign(self, recipient: str, intent: str) -> A2AMessage:
        message = A2AMessage(self.case_id, COORDINATOR, recipient, intent)
        self.trace.emit(
            case_id=self.case_id,
            event_type="task_assigned",
            actor=COORDINATOR,
            target=recipient,
            decision_code=intent,
        )
        return message

    def handoff(
        self, task: A2AMessage, result_code: str, refs: list[str], target: str = COORDINATOR
    ) -> A2AMessage:
        message = A2AMessage(self.case_id, task.recipient, target, result_code, tuple(refs))
        self.trace.emit(
            case_id=self.case_id,
            event_type="handoff",
            actor=task.recipient,
            target=target,
            decision_code=result_code,
            evidence_refs=list(refs)[:20] or None,
        )
        return message

    async def fetch(self, actor: str, tool: str, **arguments: str) -> Evidence | None:
        evidence = await self.store.fetch(actor, tool, **arguments)
        if evidence is not None:
            self.evidence[tool] = evidence
        return evidence


# ---------------------------------------------------------------- specialists


@dataclass
class EntityResult:
    status: str
    resolved: list[str]
    rejected: list[str]
    confidence: float
    customer_unique_id: str | None
    related_order_ids: list[str]
    order: OrderFacts


async def entity_agent(ctx: CaseContext) -> EntityResult:
    task = ctx.assign("entity-agent", "RESOLVE_ENTITY")
    case = ctx.case
    request = case.get("customer_request", {})
    candidates = unique(case.get("candidate_order_ids") or [])
    claimed = request.get("claimed_order_id")
    if claimed and claimed not in candidates:
        candidates.insert(0, claimed)
    hint = case.get("customer_unique_id_hint")

    history_ids: list[str] = []
    customer_id: str | None = None
    if hint:
        history = await ctx.fetch("entity-agent", "get_customer_history", customer_unique_id=hint)
        if history is not None:
            history_ids = history_order_ids(history.data)
            customer_id = hint

    # Rank: claimed first, then any other candidate the customer's history confirms.
    ranked = sorted(
        candidates, key=lambda oid: (oid != claimed, oid not in history_ids, candidates.index(oid))
    )
    resolved: list[str] = []
    order = OrderFacts()
    for candidate in ranked:
        if history_ids and candidate not in history_ids:
            continue  # rejected from customer history alone, no extra tool call
        evidence = await ctx.fetch("entity-agent", "get_order", order_id=candidate)
        if evidence is None:
            continue
        facts = order_facts(evidence.data)
        owner = facts.customer_unique_id
        if owner and hint and owner != hint:
            continue
        resolved.append(candidate)
        order = facts
        if owner:
            customer_id = owner
        break

    rejected = [oid for oid in candidates if oid not in resolved]
    in_history = bool(resolved) and resolved[0] in history_ids
    others_in_history = [oid for oid in rejected if oid in history_ids]
    if not resolved:
        status, confidence = "not_found", 0.2
    elif others_in_history and not in_history:
        status, confidence = "ambiguous", 0.4
    else:
        status = "resolved"
        confidence = 0.95 if in_history else 0.75

    refs = [
        ev.ref for key, ev in ctx.evidence.items() if key in {"get_order", "get_customer_history"}
    ]
    ctx.handoff(task, f"ENTITY_{status.upper()}", refs)
    return EntityResult(
        status=status,
        resolved=resolved,
        rejected=rejected,
        confidence=confidence,
        customer_unique_id=customer_id,
        related_order_ids=history_ids,
        order=order,
    )


async def policy_agent(ctx: CaseContext) -> PolicyFacts:
    task = ctx.assign("policy-agent", "LOAD_POLICY")
    version = ctx.case.get("policy_version")
    evidence = (
        await ctx.fetch("policy-agent", "get_policy", policy_version=version) if version else None
    )
    ctx.handoff(task, "POLICY_LOADED" if evidence else "POLICY_MISSING", _refs(evidence))
    return policy_facts(evidence.data) if evidence else PolicyFacts()


async def payment_agent(ctx: CaseContext, order_id: str) -> tuple[PaymentFacts, RefundFacts]:
    task = ctx.assign("payment-agent", "ANALYZE_PAYMENT")
    payments = await ctx.fetch("payment-agent", "get_payment_timeline", order_id=order_id)
    if payments is None:
        # Fallback source when the authoritative timeline is unavailable.
        payments = await ctx.fetch("payment-agent", "get_order_payments", order_id=order_id)
    refunds = await ctx.fetch("payment-agent", "get_refund_timeline", order_id=order_id)
    pay = payment_facts(payments.data) if payments else PaymentFacts()
    ref = refund_facts(refunds.data) if refunds else RefundFacts()
    code = "PAYMENT_ANALYZED" if payments else "PAYMENT_EVIDENCE_MISSING"
    ctx.handoff(task, code, _refs(payments, refunds))
    return pay, ref


async def order_agent(ctx: CaseContext, order_id: str) -> ItemFacts | None:
    task = ctx.assign("order-agent", "ANALYZE_ITEMS")
    items = await ctx.fetch("order-agent", "get_order_items", order_id=order_id)
    ctx.handoff(task, "ITEMS_ANALYZED" if items else "ITEMS_MISSING", _refs(items))
    return item_facts(items.data) if items else None


async def shipment_agent(
    ctx: CaseContext, order_id: str, order: OrderFacts, prefer_order_row: bool
) -> tuple[ShipmentFacts, ItemFacts | None]:
    task = ctx.assign("shipment-agent", "ANALYZE_SHIPMENT")
    shipment = await ctx.fetch("shipment-agent", "get_shipment_summary", order_id=order_id)
    data = shipment.data if shipment else {}
    facts = shipment_facts(data, order, None, prefer_order_row)
    items: ItemFacts | None = None
    # Item rows (handoff limits, freight) are only needed to attribute and price a late
    # delivery, so on-time orders never pay for this call.
    if facts.verdict in {"seller_delay", "logistics_delay"}:
        items = await order_agent(ctx, order_id)
        if items is not None:
            facts = shipment_facts(data, order, items, prefer_order_row)
    ctx.handoff(task, f"SHIPMENT_{facts.verdict.upper()}", _refs(shipment))
    return facts, items


def _refs(*evidence: Evidence | None) -> list[str]:
    return [item.ref for item in evidence if item is not None]


# ---------------------------------------------------------------- conflict resolver


def source_rank(policy: PolicyFacts, source: str) -> int:
    precedence = policy.source_precedence or DEFAULT_PRECEDENCE
    return next(
        (i for i, name in enumerate(precedence) if name == source or source in name),
        len(precedence),
    )


def prefers_order_row(policy: PolicyFacts) -> bool:
    return source_rank(policy, "get_order") <= source_rank(policy, "get_shipment_summary")


def conflict_resolver(
    ctx: CaseContext, order: OrderFacts, policy: PolicyFacts
) -> list[dict[str, Any]]:
    """Compare overlapping fields across sources and select one by source precedence."""
    task = ctx.assign("conflict-resolver", "RESOLVE_CONFLICTS")

    def rank(source: str) -> int:
        return source_rank(policy, source)

    conflicts: list[dict[str, Any]] = []
    shipment = ctx.evidence.get("get_shipment_summary")
    if shipment is not None and ctx.evidence.get("get_order") is not None:
        other = order_facts(shipment.data)
        for field_name, left, right in (
            ("order_delivered_customer_date", order.delivered_at, other.delivered_at),
            ("order_delivered_carrier_date", order.carrier_at, other.carrier_at),
            ("order_estimated_delivery_date", order.estimated_at, other.estimated_at),
            ("order_status", order.status, other.status),
        ):
            if left is not None and right is not None and left != right:
                sources = ["get_order", "get_shipment_summary"]
                conflicts.append(
                    {
                        "field": field_name,
                        "sources": sources,
                        "selected_source": min(sources, key=rank),
                        "resolution_code": "SOURCE_PRECEDENCE",
                    }
                )
    refs = _refs(ctx.evidence.get("get_order"), shipment) if conflicts else []
    ctx.handoff(task, "CONFLICTS_FOUND" if conflicts else "NO_CONFLICT", refs)
    return conflicts[:5]


# ---------------------------------------------------------------- decision


@dataclass
class Findings:
    entity: EntityResult
    policy: PolicyFacts
    shipment: ShipmentFacts = field(default_factory=ShipmentFacts)
    payment: PaymentFacts = field(default_factory=PaymentFacts)
    refund: RefundFacts = field(default_factory=RefundFacts)
    items: ItemFacts | None = None
    conflicts: list[dict[str, Any]] = field(default_factory=list)


def payment_verdict(findings: Findings) -> str:
    pay, ref = findings.payment, findings.refund
    if pay.captured_total is None:
        return "insufficient_evidence"
    if pay.duplicate_amount > MONEY_TOLERANCE:
        return "duplicate_capture"
    if pay.base_total is not None and abs(pay.captured_total - pay.base_total) > MONEY_TOLERANCE:
        return "capture_mismatch"
    if ref.failed_total > MONEY_TOLERANCE and ref.refunded_total < ref.failed_total:
        return "refund_failed"
    if ref.pending_total > MONEY_TOLERANCE:
        return "refund_pending"
    if ref.refunded_total > MONEY_TOLERANCE:
        return "refunded"
    return "reconciled"


def detect_issues(findings: Findings, pay_verdict: str) -> list[str]:
    """All issues the evidence supports, in priority order."""
    status = (findings.entity.order.status or "").lower()
    pay, ref = findings.payment, findings.refund
    outstanding = (pay.captured_total or 0.0) - ref.refunded_total
    issues: list[str] = []
    if "cancel" in status and outstanding > MONEY_TOLERANCE:
        issues.append("canceled_order_paid")
    if "unavailable" in status and outstanding > MONEY_TOLERANCE:
        issues.append("unavailable_order_paid")
    if pay_verdict == "duplicate_capture":
        issues.append("duplicate_charge")
    if pay_verdict == "capture_mismatch":
        issues.append("payment_mismatch")
    if ref.failed_total > MONEY_TOLERANCE and ref.refunded_total < ref.failed_total:
        issues.append("refund_failed")
    if ref.pending_total > MONEY_TOLERANCE:
        issues.append("refund_pending")
    if findings.shipment.verdict == "seller_delay":
        issues.append("late_delivery_seller")
    elif findings.shipment.verdict in {"logistics_delay", "lost"}:
        issues.append("late_delivery_logistics")
    if pay_verdict in {"reconciled", "refunded"} and pay.base_rows > 1:
        issues.append("valid_split_payment")
    return issues


def refund_plan(
    issue: str, findings: Findings, order_id: str
) -> tuple[float, list[dict[str, Any]], list[str]]:
    pay, ref, policy = findings.payment, findings.refund, findings.policy
    captured = pay.captured_total or 0.0
    outstanding = max(captured - ref.refunded_total - ref.pending_total, 0.0)
    lines: list[dict[str, Any]] = []
    actions: list[str] = []
    if issue in {"canceled_order_paid", "unavailable_order_paid"}:
        reason = (
            "CANCELED_ORDER_REFUND" if issue.startswith("canceled") else "UNAVAILABLE_ITEM_REFUND"
        )
        lines.append({"reason_code": reason, "amount_brl": r2(outstanding), "entity_id": order_id})
        actions = ["refund_customer", "close_order_payment"]
    elif issue == "duplicate_charge":
        entity = pay.duplicate_refs[0] if pay.duplicate_refs else order_id
        lines.append(
            {
                "reason_code": "DUPLICATE_CAPTURE_REVERSAL",
                "amount_brl": pay.duplicate_amount,
                "entity_id": entity,
            }
        )
        actions = ["reverse_duplicate_capture", "notify_payment_provider"]
    elif issue == "payment_mismatch":
        excess = r2(captured - (pay.base_total or captured))
        if excess > MONEY_TOLERANCE:
            lines.append(
                {"reason_code": "OVERCAPTURE_REFUND", "amount_brl": excess, "entity_id": order_id}
            )
        actions = ["reconcile_payment_capture", "notify_payment_provider"]
    elif issue == "refund_failed":
        entity = ref.refund_refs[0] if ref.refund_refs else order_id
        lines.append(
            {
                "reason_code": "FAILED_REFUND_RETRY",
                "amount_brl": ref.failed_total,
                "entity_id": entity,
            }
        )
        actions = ["retry_failed_refund", "notify_payment_provider"]
    elif issue == "refund_pending":
        entity = ref.refund_refs[0] if ref.refund_refs else order_id
        lines.append(
            {
                "reason_code": "PENDING_REFUND_COMPLETION",
                "amount_brl": ref.pending_total,
                "entity_id": entity,
            }
        )
        actions = ["expedite_pending_refund"]
    elif issue in LATE_ISSUES:
        amount = 0.0
        if policy.late_refund_ratio is not None:
            amount = r2(captured * policy.late_refund_ratio)
        elif policy.late_refund_freight and findings.items and findings.items.freight_total:
            amount = findings.items.freight_total
        if amount > MONEY_TOLERANCE and findings.shipment.days_late > policy.late_grace_days:
            lines.append(
                {
                    "reason_code": "LATE_DELIVERY_COMPENSATION",
                    "amount_brl": r2(amount),
                    "entity_id": order_id,
                }
            )
        if issue == "late_delivery_seller":
            actions = ["compensate_customer", "notify_seller"]
        else:
            actions = ["compensate_customer", "escalate_logistics_provider"]
        if not lines:
            actions = [action for action in actions if action != "compensate_customer"]
    total = r2(sum(line["amount_brl"] for line in lines))
    return total, [line for line in lines if line["amount_brl"] > 0], actions


def root_cause(issue: str, findings: Findings) -> dict[str, Any]:
    causes: dict[str, tuple[str, list[dict[str, Any]]]] = {
        "late_delivery_seller": (
            "SELLER_LATE_HANDOFF",
            [{"party_type": "seller", "party_id": s} for s in findings.shipment.late_seller_ids],
        ),
        "late_delivery_logistics": (
            "LOGISTICS_TRANSIT_DELAY",
            [{"party_type": "logistics_provider", "party_id": None}],
        ),
        "canceled_order_paid": (
            "CANCELED_ORDER_NOT_REFUNDED",
            [{"party_type": "platform", "party_id": None}],
        ),
        "unavailable_order_paid": (
            "UNAVAILABLE_ORDER_NOT_REFUNDED",
            [{"party_type": "platform", "party_id": None}],
        ),
        "duplicate_charge": (
            "DUPLICATE_PAYMENT_CAPTURE",
            [{"party_type": "payment_provider", "party_id": None}],
        ),
        "payment_mismatch": (
            "PAYMENT_CAPTURE_MISMATCH",
            [{"party_type": "payment_provider", "party_id": None}],
        ),
        "refund_failed": (
            "REFUND_PROCESSING_FAILED",
            [{"party_type": "payment_provider", "party_id": None}],
        ),
        "refund_pending": (
            "REFUND_NOT_COMPLETED",
            [{"party_type": "platform", "party_id": None}],
        ),
        "valid_split_payment": ("VALID_SPLIT_PAYMENT", []),
        "unsupported_claim": ("CLAIM_NOT_SUPPORTED_BY_EVIDENCE", []),
        "insufficient_evidence": (
            "INSUFFICIENT_EVIDENCE",
            [{"party_type": "unknown", "party_id": None}],
        ),
    }
    code, parties = causes[issue]
    return {"ranked_causes": [{"cause_code": code, "rank": 1}], "responsible_parties": parties[:5]}


# ---------------------------------------------------------------- verifier


def verifier(ctx: CaseContext, output: dict[str, Any]) -> dict[str, Any]:
    """Independent checks over the draft; adjusts only toward safer, consistent values."""
    task = ctx.assign("verifier", "VERIFY_OUTPUT")
    adjustments = 0
    candidates = set(unique(ctx.case.get("candidate_order_ids") or []))
    claimed = ctx.case.get("customer_request", {}).get("claimed_order_id")
    if claimed:
        candidates.add(claimed)
    er = output["entity_resolution"]
    if not set(er["resolved_order_ids"]) <= candidates or set(er["resolved_order_ids"]) & set(
        er["rejected_candidates"]
    ):
        raise ValueError(f"{ctx.case_id}: entity scope violated")

    owned = {ev.ref for ev in ctx.evidence.values()}
    output["evidence_refs"] = [ref for ref in output["evidence_refs"] if ref in owned][:30]
    for claim in output.get("claim_assessments", []):
        claim["evidence_refs"] = [ref for ref in claim["evidence_refs"] if ref in owned][:30]

    financial = output["financial_resolution"]
    lines_total = r2(sum(line["amount_brl"] for line in financial["refund_lines"]))
    if abs(lines_total - financial["recommended_refund_brl"]) > MONEY_TOLERANCE:
        financial["recommended_refund_brl"] = lines_total
        adjustments += 1
    cap = output["payment_analysis"]["captured_total_brl"]
    if cap is not None and financial["recommended_refund_brl"] > cap + MONEY_TOLERANCE:
        financial["recommended_refund_brl"] = 0.0
        financial["refund_lines"] = []
        output["assessment"]["case_status"] = "needs_investigation"
        adjustments += 1

    status = output["assessment"]["case_status"]
    if status == "no_action" and (
        financial["recommended_refund_brl"] > 0 or output["resolution_actions"]
    ):
        financial["recommended_refund_brl"], financial["refund_lines"] = 0.0, []
        output["resolution_actions"] = []
        adjustments += 1
    if status == "action_required" and not output["resolution_actions"]:
        output["assessment"]["case_status"] = "needs_investigation"
        adjustments += 1
    if (
        output["assessment"]["case_status"] == "needs_investigation"
        and not output["resolution_actions"]
    ):
        output["resolution_actions"] = ["request_additional_evidence"]

    output["resolution_actions"] = unique(output["resolution_actions"])[:8]
    assessment = output["assessment"]
    assessment["confidence"] = min(max(assessment["confidence"], 0.0), 1.0)
    late_sellers = output["shipment_analysis"]["late_seller_ids"]
    if assessment["primary_issue"] == "late_delivery_seller" and not late_sellers:
        raise ValueError(f"{ctx.case_id}: seller responsibility without late seller evidence")
    code = "VERIFIED" if adjustments == 0 else "VERIFIED_WITH_ADJUSTMENTS"
    ctx.trace.emit(
        case_id=ctx.case_id,
        event_type="verification_completed",
        actor="verifier",
        target=COORDINATOR,
        decision_code=code,
        evidence_refs=output["evidence_refs"][:20] or None,
        attributes={"adjustments": adjustments, "evidence_count": len(output["evidence_refs"])},
    )
    ctx.handoff(task, code, [])
    return output


# ---------------------------------------------------------------- coordinator


def claim_assessments(
    ctx: CaseContext,
    issues: list[str],
    primary: str,
    refund: float,
    captured: float,
    refs_for: dict[str, list[str]],
) -> list[dict[str, Any]]:
    result = []
    for claim in (ctx.case.get("customer_request", {}).get("claims") or [])[:5]:
        topic = claim.get("topic", "")
        if primary == "insufficient_evidence":
            verdict, confidence = "insufficient_evidence", 0.4
        elif topic == "requested_full_refund":
            if captured > 0 and refund >= captured - MONEY_TOLERANCE:
                verdict, confidence = "supported", 0.8
            elif refund > MONEY_TOLERANCE:
                verdict, confidence = "partially_supported", 0.75
            else:
                verdict, confidence = "unsupported", 0.8
        elif topic in issues:
            verdict, confidence = "supported", 0.85
        else:
            verdict, confidence = "unsupported", 0.75
        result.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": refs_for.get(topic, refs_for["base"]),
            }
        )
    return result


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Coordinator: entity resolution → specialists → conflict resolver → verifier."""
    ctx = CaseContext(case, CaseEvidenceStore(case["case_id"], gateway, trace), trace)
    claims = [c.get("topic") for c in (case.get("customer_request", {}).get("claims") or [])]
    claimed_topic = next((t for t in claims if t != "requested_full_refund"), None)

    entity = await entity_agent(ctx)
    policy = await policy_agent(ctx)
    findings = Findings(entity=entity, policy=policy)
    order_id = entity.resolved[0] if entity.resolved else None

    if order_id and entity.status == "resolved":
        findings.shipment, findings.items = await shipment_agent(
            ctx, order_id, entity.order, prefers_order_row(policy)
        )
        findings.payment, findings.refund = await payment_agent(ctx, order_id)
        findings.conflicts = conflict_resolver(ctx, entity.order, policy)

    pay_verdict = payment_verdict(findings) if order_id else "insufficient_evidence"
    issues = detect_issues(findings, pay_verdict) if order_id else []
    if entity.status != "resolved":
        primary = "insufficient_evidence"
    elif claimed_topic in issues:
        primary = claimed_topic
    elif issues:
        primary = issues[0]
    elif (
        findings.shipment.verdict == "insufficient_evidence"
        and pay_verdict == "insufficient_evidence"
    ):
        primary = "insufficient_evidence"
    else:
        primary = "unsupported_claim"
    secondary = [issue for issue in issues if issue != primary][:10]

    refund_total, refund_lines, actions = (
        refund_plan(primary, findings, order_id) if order_id else (0.0, [], [])
    )
    if primary == "insufficient_evidence":
        status = "needs_investigation"
    elif primary in NO_ACTION_ISSUES:
        status = "no_action"
    else:
        status = "action_required" if actions else "needs_investigation"
    ctx.trace.emit(
        case_id=ctx.case_id,
        event_type="policy_decided",
        actor="policy-agent",
        target=COORDINATOR,
        decision_code=primary.upper(),
        evidence_refs=_refs(ctx.evidence.get("get_policy")) or None,
        attributes={"case_status": status, "secondary_issues": len(secondary)},
    )

    confidence = 0.9 if primary == claimed_topic else 0.75
    if primary == "unsupported_claim":
        confidence = 0.7
    if primary == "insufficient_evidence":
        confidence = 0.35
    confidence -= 0.05 * len(ctx.store.failures) + 0.05 * sum(
        1 for c in findings.conflicts if c["selected_source"] is None
    )
    confidence = r2(min(max(confidence, 0.05), 0.95))

    def refs_for_domains(domains: set[str]) -> list[str]:
        return unique(ev.ref for ev in ctx.evidence.values() if ev.domain in domains)

    relevant = (
        BASE_DOMAINS
        | ISSUE_DOMAINS.get(primary, set())
        | ISSUE_DOMAINS.get(claimed_topic or "", set())
    )
    for issue in secondary:
        relevant |= ISSUE_DOMAINS.get(issue, set())
    refs_by_topic = {
        topic: refs_for_domains(BASE_DOMAINS | ISSUE_DOMAINS.get(topic, set()))
        for topic in claims
        if topic
    }
    refs_by_topic["base"] = refs_for_domains(BASE_DOMAINS)

    pay, ref = findings.payment, findings.refund
    captured = pay.captured_total
    items = findings.items
    output: dict[str, Any] = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": ctx.case_id,
        "assessment": {
            "primary_issue": primary,
            "secondary_issues": secondary,
            "case_status": status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": entity.resolved[:20],
            "item_ids": (items.item_ids if items else [])[:20],
            "seller_ids": unique(
                [*findings.shipment.seller_ids, *(items.seller_ids if items else [])]
            )[:20],
            "payment_references": pay.payment_refs[:20],
            "shipment_ids": findings.shipment.shipment_ids[:20],
        },
        "claim_assessments": claim_assessments(
            ctx, issues, primary, refund_total, captured or 0.0, refs_by_topic
        ),
        "entity_resolution": {
            "status": entity.status,
            "resolved_order_ids": entity.resolved[:20],
            "rejected_candidates": entity.rejected[:20],
            "confidence": entity.confidence,
        },
        "customer_context": {
            "customer_unique_id": entity.customer_unique_id,
            "related_order_ids": entity.related_order_ids[:20],
        },
        "shipment_analysis": {
            "verdict": findings.shipment.verdict,
            "late_seller_ids": findings.shipment.late_seller_ids[:20],
            "timeline_complete": findings.shipment.timeline_complete,
        },
        "payment_analysis": {
            "verdict": pay_verdict,
            "captured_total_brl": captured,
            "refunded_total_brl": ref.refunded_total if captured is not None else None,
            "refundable_total_brl": (
                r2(max(captured - ref.refunded_total, 0.0)) if captured is not None else None
            ),
        },
        "root_cause_analysis": root_cause(primary, findings),
        "evidence_refs": refs_for_domains(relevant)[:30],
        "data_conflicts": findings.conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund_total,
            "refund_lines": refund_lines[:10],
        },
        "resolution_actions": actions,
    }
    return verifier(ctx, output)
