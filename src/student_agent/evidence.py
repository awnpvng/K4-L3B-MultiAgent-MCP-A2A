from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

# Least privilege: each actor may only call the tools its role needs.
TOOL_PERMISSIONS: dict[str, frozenset[str]] = {
    "entity-agent": frozenset({"get_order", "get_customer_history"}),
    "order-agent": frozenset({"get_order_items", "get_sellers", "get_product_context"}),
    "shipment-agent": frozenset({"get_shipment_summary"}),
    "payment-agent": frozenset(
        {"get_order_payments", "get_payment_timeline", "get_refund_timeline"}
    ),
    "policy-agent": frozenset({"get_policy"}),
}

# Transport failures are retried once; tool errors are deterministic and never retried.
TRANSPORT_RETRIES = 1
CALL_TIMEOUT_SECONDS = 120.0


class ToolPermissionError(PermissionError):
    pass


@dataclass
class Evidence:
    tool: str
    ref: str
    domain: str
    data: Any
    warnings: tuple[str, ...] = ()


@dataclass
class CaseEvidenceStore:
    """Per-case evidence cache. A new store is created for every case, so refs never leak."""

    case_id: str
    gateway: EvidenceGateway
    trace: TraceWriter
    _cache: dict[tuple[str, tuple[tuple[str, str], ...]], Evidence | None] = field(
        default_factory=dict
    )
    failures: list[str] = field(default_factory=list)
    calls: int = 0

    async def fetch(self, actor: str, tool: str, **arguments: str) -> Evidence | None:
        if tool not in TOOL_PERMISSIONS.get(actor, frozenset()):
            raise ToolPermissionError(f"{actor} is not allowed to call {tool}")
        key = (tool, tuple(sorted(arguments.items())))
        if key in self._cache:
            return self._cache[key]
        evidence: Evidence | None = None
        for attempt in range(TRANSPORT_RETRIES + 1):
            self.calls += 1
            try:
                raw = await asyncio.wait_for(
                    self.gateway.call(tool, case_id=self.case_id, **arguments),
                    CALL_TIMEOUT_SECONDS,
                )
            except RuntimeError:
                # The gateway reported a tool error (not found / out of scope): no retry.
                self.failures.append(f"{tool}:tool_error")
                break
            except ValueError:
                # Response violated the evidence contract; retrying would return the same.
                self.failures.append(f"{tool}:invalid_response")
                break
            except Exception as exc:  # noqa: BLE001 - transport errors vary by client version
                if attempt >= TRANSPORT_RETRIES:
                    self.failures.append(f"{tool}:{type(exc).__name__}")
                continue
            evidence = Evidence(
                tool=tool,
                ref=raw["evidence_ref"],
                domain=raw["domain"],
                data=raw["data"],
                warnings=tuple(raw.get("warnings") or ()),
            )
            self.trace.emit(
                case_id=self.case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool,
                evidence_refs=[evidence.ref],
                attributes={"domain": evidence.domain, "warnings": len(evidence.warnings)},
            )
            break
        self._cache[key] = evidence
        return evidence
