"""Deterministic specialist analysis over MCP evidence payloads.

The payload layout of each tool is not part of the public contract, so every reader here
looks fields up by their Olist column names anywhere in the nested payload instead of
assuming one fixed shape. Missing evidence always degrades to ``insufficient_evidence``;
nothing is invented.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

MONEY_TOLERANCE = 0.01

# ---------------------------------------------------------------- generic payload access


def walk(value: Any) -> Iterator[dict[str, Any]]:
    """Yield every dict nested anywhere inside ``value`` (including ``value`` itself)."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def collect(value: Any, *keys: str) -> list[Any]:
    """All non-null values stored under any of ``keys`` anywhere in ``value``, in order."""
    found: list[Any] = []
    for node in walk(value):
        for key in keys:
            item = node.get(key)
            if item is not None and item != "":
                found.append(item)
    return found


def first(value: Any, *keys: str) -> Any:
    items = collect(value, *keys)
    return items[0] if items else None


def unique(values: Iterable[Any]) -> list[str]:
    seen: dict[str, None] = {}
    for item in values:
        if isinstance(item, str | int) and not isinstance(item, bool):
            seen.setdefault(str(item), None)
    return list(seen)


def rows_with(value: Any, *keys: str) -> list[dict[str, Any]]:
    """Dicts that carry at least one of ``keys`` (e.g. event rows)."""
    return [node for node in walk(value) if any(key in node for key in keys)]


def money(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "."))
        except ValueError:
            return None
    return None


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    for candidate in (text, text.replace(" ", "T", 1)):
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        return parsed.replace(tzinfo=None)
    return None


def text_of(row: dict[str, Any], *keys: str) -> str:
    return " ".join(str(row.get(key, "")) for key in keys).lower()


def r2(value: float) -> float:
    return round(value + 0.0, 2)


# ---------------------------------------------------------------- order / entity


@dataclass
class OrderFacts:
    order_id: str | None = None
    status: str | None = None
    customer_unique_id: str | None = None
    purchased_at: datetime | None = None
    carrier_at: datetime | None = None
    delivered_at: datetime | None = None
    estimated_at: datetime | None = None
    order_total: float | None = None


def order_facts(data: Any) -> OrderFacts:
    return OrderFacts(
        order_id=first(data, "order_id"),
        status=(first(data, "order_status", "status") or None),
        customer_unique_id=first(data, "customer_unique_id"),
        purchased_at=parse_time(first(data, "order_purchase_timestamp", "purchased_at")),
        carrier_at=parse_time(first(data, "order_delivered_carrier_date", "carrier_handoff_at")),
        delivered_at=parse_time(first(data, "order_delivered_customer_date", "delivered_at")),
        estimated_at=parse_time(first(data, "order_estimated_delivery_date", "estimated_at")),
        order_total=money(first(data, "order_total", "order_total_brl", "total_brl")),
    )


def history_order_ids(data: Any) -> list[str]:
    return unique(collect(data, "order_id"))


# ---------------------------------------------------------------- items / sellers


@dataclass
class ItemFacts:
    item_ids: list[str] = field(default_factory=list)
    seller_ids: list[str] = field(default_factory=list)
    product_ids: list[str] = field(default_factory=list)
    goods_total: float | None = None
    freight_total: float | None = None
    shipping_limits: dict[str, datetime] = field(default_factory=dict)

    @property
    def expected_total(self) -> float | None:
        if self.goods_total is None:
            return None
        return r2(self.goods_total + (self.freight_total or 0.0))


def item_facts(data: Any) -> ItemFacts:
    rows = rows_with(data, "price", "order_item_id")
    facts = ItemFacts()
    prices = [money(row.get("price")) for row in rows]
    freights = [money(row.get("freight_value")) for row in rows]
    if rows and all(price is not None for price in prices):
        facts.goods_total = r2(sum(price for price in prices if price is not None))
        facts.freight_total = r2(sum(value for value in freights if value is not None))
    for row in rows:
        order_id, item_no = row.get("order_id"), row.get("order_item_id")
        item_id = row.get("item_id") or (
            f"{order_id}-{item_no}" if order_id and item_no is not None else item_no
        )
        facts.item_ids.extend(unique([item_id]))
    facts.seller_ids = unique(collect(data, "seller_id"))
    facts.product_ids = unique(collect(data, "product_id"))
    facts.shipping_limits = shipping_limits(data)
    facts.item_ids = unique(facts.item_ids)
    return facts


def shipping_limits(data: Any) -> dict[str, datetime]:
    """Earliest seller handoff deadline per seller."""
    limits: dict[str, datetime] = {}
    for row in rows_with(data, "shipping_limit_date", "handoff_limit", "seller_handoff_limit"):
        seller = row.get("seller_id")
        limit = parse_time(
            row.get("shipping_limit_date")
            or row.get("handoff_limit")
            or row.get("seller_handoff_limit")
        )
        if seller and limit and (seller not in limits or limit < limits[seller]):
            limits[str(seller)] = limit
    return limits


# ---------------------------------------------------------------- shipment

LOST_WORDS = ("lost", "extravi")
RETURN_WORDS = ("returned", "return_to_sender", "devolvid")


@dataclass
class ShipmentFacts:
    verdict: str = "insufficient_evidence"
    late_seller_ids: list[str] = field(default_factory=list)
    timeline_complete: bool = False
    shipment_ids: list[str] = field(default_factory=list)
    seller_ids: list[str] = field(default_factory=list)
    delivered_at: datetime | None = None
    days_late: float = 0.0


def _pick(primary: datetime | None, secondary: datetime | None) -> datetime | None:
    return primary if primary is not None else secondary


def shipment_facts(
    data: Any, order: OrderFacts, items: ItemFacts | None, prefer_order_row: bool = False
) -> ShipmentFacts:
    """Delivery verdict; ``prefer_order_row`` applies source precedence on timestamp conflicts."""
    facts = ShipmentFacts()
    facts.shipment_ids = unique(collect(data, "shipment_id", "tracking_id"))
    facts.seller_ids = unique(collect(data, "seller_id"))
    summary_carrier = parse_time(
        first(data, "order_delivered_carrier_date", "carrier_handoff_at", "handoff_at")
    )
    summary_delivered = parse_time(first(data, "order_delivered_customer_date", "delivered_at"))
    summary_estimated = parse_time(
        first(data, "order_estimated_delivery_date", "estimated_delivery_date")
    )
    if prefer_order_row:
        carrier_at = _pick(order.carrier_at, summary_carrier)
        delivered_at = _pick(order.delivered_at, summary_delivered)
        estimated_at = _pick(order.estimated_at, summary_estimated)
    else:
        carrier_at = _pick(summary_carrier, order.carrier_at)
        delivered_at = _pick(summary_delivered, order.delivered_at)
        estimated_at = _pick(summary_estimated, order.estimated_at)
    facts.delivered_at = delivered_at
    purchased_at = order.purchased_at or parse_time(first(data, "order_purchase_timestamp"))
    facts.timeline_complete = all((purchased_at, carrier_at, delivered_at, estimated_at))

    event_text = (
        " ".join(
            text_of(row, "status", "event", "event_type", "type")
            for row in rows_with(data, "status", "event", "event_type")
        )
        + f" {order.status or ''}".lower()
    )
    if any(word in event_text for word in LOST_WORDS):
        facts.verdict = "lost"
        return facts
    if any(word in event_text for word in RETURN_WORDS):
        facts.verdict = "returned"
        return facts
    if delivered_at is None or estimated_at is None:
        return facts

    limits = shipping_limits(data) or (items.shipping_limits if items else {})
    facts.days_late = max((delivered_at - estimated_at).total_seconds() / 86400, 0.0)
    late_sellers = (
        sorted(seller for seller, limit in limits.items() if carrier_at and carrier_at > limit)
        if carrier_at
        else []
    )
    if delivered_at <= estimated_at:
        facts.verdict = "on_time"
    elif late_sellers:
        facts.verdict = "seller_delay"
        facts.late_seller_ids = late_sellers
    elif carrier_at is not None and limits:
        facts.verdict = "logistics_delay"
    elif carrier_at is not None:
        # Late without any seller handoff limit to compare against: carrier leg is late.
        facts.verdict = "logistics_delay"
    return facts


# ---------------------------------------------------------------- payment / refund

SUCCESS_WORDS = ("captur", "settled", "succeed", "success", "paid", "approved", "complete")
FAIL_WORDS = ("fail", "declin", "reject", "error", "revers", "void", "chargeback")
PENDING_WORDS = ("pending", "request", "processing", "initiated", "submitted", "in_progress")


@dataclass
class PaymentFacts:
    base_total: float | None = None
    base_rows: int = 0
    captured_total: float | None = None
    duplicate_amount: float = 0.0
    duplicate_refs: list[str] = field(default_factory=list)
    payment_refs: list[str] = field(default_factory=list)
    has_events: bool = False


def _event_kind(row: dict[str, Any]) -> str:
    return text_of(row, "event_type", "type", "event", "action", "kind")


def _status(row: dict[str, Any]) -> str:
    return text_of(row, "status", "state", "outcome", "result")


def payment_facts(data: Any) -> PaymentFacts:
    facts = PaymentFacts()
    base = rows_with(data, "payment_value")
    base_values = [money(row.get("payment_value")) for row in base]
    if base and all(value is not None for value in base_values):
        facts.base_rows = len(base)
        facts.base_total = r2(sum(value for value in base_values if value is not None))
    facts.payment_refs = unique(
        collect(data, "payment_reference", "payment_id", "transaction_id", "payment_ref")
    )

    events = [
        row
        for row in rows_with(data, "event_type", "event", "type", "action")
        if "payment_value" not in row or "amount" in row or "amount_brl" in row
    ]
    captures: list[tuple[str, float]] = []
    for row in events:
        kind, status = _event_kind(row), _status(row)
        amount = money(row.get("amount_brl", row.get("amount")))
        if amount is None or "refund" in kind:
            continue
        if "captur" in kind and not any(word in status for word in FAIL_WORDS + PENDING_WORDS):
            ref = str(
                row.get("payment_reference")
                or row.get("payment_id")
                or row.get("payment_sequential")
                or ""
            )
            captures.append((ref, amount))
    if events:
        facts.has_events = True
    if captures:
        facts.captured_total = r2(sum(amount for _, amount in captures))
        seen: dict[tuple[str, float], int] = {}
        for ref, amount in captures:
            key = (ref, round(amount, 2))
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 1:
                facts.duplicate_amount = r2(facts.duplicate_amount + amount)
                if ref:
                    facts.duplicate_refs = unique([*facts.duplicate_refs, ref])
    elif facts.base_total is not None:
        # No lifecycle capture events: the base payment rows are the only capture record.
        facts.captured_total = facts.base_total
    return facts


@dataclass
class RefundFacts:
    refunded_total: float = 0.0
    pending_total: float = 0.0
    failed_total: float = 0.0
    latest_status: str | None = None
    refund_refs: list[str] = field(default_factory=list)
    has_events: bool = False


def refund_facts(data: Any) -> RefundFacts:
    facts = RefundFacts()
    facts.refund_refs = unique(collect(data, "refund_id", "refund_reference"))
    rows = rows_with(data, "status", "state", "event_type", "event")
    latest: dict[str, tuple[datetime | None, str, float]] = {}
    for index, row in enumerate(rows):
        status = _status(row) or _event_kind(row)
        amount = money(row.get("amount_brl", row.get("amount", row.get("refund_amount"))))
        if not status or amount is None:
            continue
        facts.has_events = True
        key = str(row.get("refund_id") or row.get("refund_reference") or f"row-{index}")
        when = parse_time(row.get("occurred_at") or row.get("timestamp") or row.get("created_at"))
        previous = latest.get(key)
        if previous is None or (when and (previous[0] is None or when >= previous[0])):
            latest[key] = (when, status, amount)
    ordered = sorted(latest.values(), key=lambda item: item[0] or datetime.min)
    for _, status, amount in ordered:
        if any(word in status for word in FAIL_WORDS):
            facts.failed_total = r2(facts.failed_total + amount)
        elif any(word in status for word in PENDING_WORDS):
            facts.pending_total = r2(facts.pending_total + amount)
        elif any(word in status for word in SUCCESS_WORDS + ("refunded",)):
            facts.refunded_total = r2(facts.refunded_total + amount)
    if ordered:
        facts.latest_status = ordered[-1][1]
    return facts


# ---------------------------------------------------------------- policy


@dataclass
class PolicyFacts:
    source_precedence: list[str] = field(default_factory=list)
    late_refund_freight: bool = True
    late_refund_ratio: float | None = None
    late_grace_days: float = 0.0


def policy_facts(data: Any) -> PolicyFacts:
    facts = PolicyFacts()
    for node in walk(data):
        for key, value in node.items():
            lowered = key.lower()
            if "precedence" in lowered and isinstance(value, list):
                facts.source_precedence = [str(item) for item in value]
            elif "grace" in lowered and money(value) is not None:
                facts.late_grace_days = money(value) or 0.0
            elif "late" in lowered and ("ratio" in lowered or "percent" in lowered):
                ratio = money(value)
                if ratio is not None:
                    facts.late_refund_ratio = ratio / 100 if ratio > 1 else ratio
            elif "late" in lowered and "freight" in lowered and isinstance(value, bool):
                facts.late_refund_freight = value
    return facts
