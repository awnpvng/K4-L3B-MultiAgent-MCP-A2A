# L3B Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Workflow là deterministic (không dùng LLM), mỗi case chạy tuần tự trong một `CaseContext` riêng.

```text
case input
   │
   ▼
Coordinator ──task_assigned──► Entity agent ──(get_customer_history, get_order)──► handoff
   │                                                    │
   │  status ≠ resolved ────────────────────────────────┴──► insufficient_evidence
   │
   ├──► Policy agent    (get_policy)                                   ──► handoff
   ├──► Shipment agent  (get_shipment_summary)
   │        └─ late? ──► Order agent (get_order_items)                 ──► handoff
   ├──► Payment agent   (get_payment_timeline [fallback get_order_payments], get_refund_timeline)
   ├──► Conflict resolver (so sánh get_order ↔ get_shipment_summary)   ──► handoff
   ├──► policy_decided  (primary issue, case_status)
   └──► Verifier (invariants, chỉnh về giá trị an toàn)  ──► verification_completed
                                                              │
                                                              ▼
                                                   outputs/<case_id>.json
Mọi MCP result ──► CaseEvidenceStore ──► tool_result_consumed (trace)
```

Code: `workflow.py` (coordinator, agents, verifier), `analysis.py` (logic nghiệp vụ thuần),
`evidence.py` (cache, quyền, retry).

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity/customer | candidates, claimed order, customer hint | Xếp hạng/loại candidate, xác định customer | `get_customer_history`, `get_order` | `ENTITY_RESOLVED/AMBIGUOUS/NOT_FOUND` + refs |
| Coordinator | case input, handoff của agent | Giao task, chọn primary issue, lập refund/action | không gọi tool | `task_assigned`, `policy_decided` |
| Order/product | order_id | Item, seller, freight, shipping limit | `get_order_items`, `get_sellers`, `get_product_context` | `ITEMS_ANALYZED` |
| Shipment | order_id, order row | Verdict giao hàng, seller trễ handoff | `get_shipment_summary` | `SHIPMENT_<VERDICT>` |
| Payment/refund | order_id | Captured/refunded/pending/failed, duplicate capture | `get_payment_timeline`, `get_order_payments`, `get_refund_timeline` | `PAYMENT_ANALYZED` |
| Policy | policy_version | Source precedence, rule bồi hoàn trễ | `get_policy` | `POLICY_LOADED`, `policy_decided` |
| Conflict resolver | evidence đã cache | Phát hiện field lệch giữa nguồn, chọn theo precedence | không gọi tool | `CONFLICTS_FOUND/NO_CONFLICT` |
| Verifier | output nháp | Kiểm tra invariant, chỉnh về giá trị an toàn | không gọi tool | `verification_completed` (`VERIFIED[_WITH_ADJUSTMENTS]`) |

Quyền được enforce trong `evidence.TOOL_PERMISSIONS`; gọi sai quyền sẽ ném `ToolPermissionError`.

## 3. Entity resolution và A2A protocol

- Gọi `get_customer_history(customer_unique_id_hint)` trước. Candidate không có trong history bị
  reject ngay mà **không** tốn thêm `get_order` (ví dụ decoy `candidate-NNN`).
- Xếp hạng: claimed order trước, rồi candidate có trong history. Candidate được chấp nhận khi
  `get_order` thành công và `customer_unique_id` của order (nếu có) khớp hint.
- `resolved` (confidence 0.95 nếu có trong history, 0.75 nếu không), `ambiguous` (0.4) khi order
  được chọn không có trong history nhưng candidate khác có, `not_found` (0.2) khi không resolve được.
- Message envelope `A2AMessage(case_id, sender, recipient, intent, evidence_refs)`; mỗi task là một
  cặp `task_assigned` → `handoff` trên trace, correlation theo `case_id`. Không có vòng lặp: luồng
  là DAG cố định, mỗi agent chạy tối đa một lần/case; mỗi call có timeout 120 s.

## 4. Evidence và conflict lifecycle

- `EvidenceGateway.call` validate envelope theo `mcp-evidence-response-v1`. `CaseEvidenceStore`
  giữ `evidence_ref` nguyên văn, cache theo `(tool, args)` và được tạo mới cho mỗi case nên ref
  không thể dùng chéo case. Mỗi kết quả emit một `tool_result_consumed` kèm ref và domain.
- `evidence_refs` trong output chỉ gồm domain liên quan: `order`, `customer`, `policy` cộng domain
  của primary/secondary issue và claim topic (`ISSUE_DOMAINS`) để giữ precision.
- Conflict: so sánh ngày giao/carrier/estimated và status giữa `get_order` và
  `get_shipment_summary`. Chọn nguồn theo `source_precedence` của policy nếu có, không thì
  `DEFAULT_PRECEDENCE` (timeline > order row > summary). Conflict không chọn được nguồn sẽ có
  `selected_source = null` và làm giảm confidence.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout / transport | 1 | bỏ evidence, degrade verdict | `handoff` `*_MISSING` |
| Tool error (not found/out of scope) | 0 | candidate kế tiếp / `get_order_payments` | `handoff` `ENTITY_NOT_FOUND` |
| Entity not found/ambiguous | 0 | `insufficient_evidence`, `needs_investigation` | `ENTITY_NOT_FOUND/AMBIGUOUS` |
| Source conflict | 0 | policy precedence | `CONFLICTS_FOUND` |
| Invalid specialist result | 0 | verifier chỉnh về an toàn hoặc raise | `VERIFIED_WITH_ADJUSTMENTS` |

Budget điển hình: 6 call/case (history, order, policy, shipment, payment timeline, refund
timeline), 7 khi giao trễ (thêm `get_order_items`). Không gọi `get_sellers`,
`get_product_context`, `get_order_payments` trừ fallback. Missing evidence không bao giờ được thay
bằng dữ liệu phỏng đoán.

## 6. Verification invariants

- Output pass JSON Schema (CLI validate lại trước khi ghi).
- `resolved_order_ids ⊆ candidates ∪ {claimed}`, không giao với `rejected_candidates`.
- Mọi `evidence_ref` (output và claim) thuộc store của case hiện tại.
- `recommended_refund_brl == Σ refund_lines` và `≤ captured_total_brl`.
- `no_action` ⇒ refund 0 và không có action; `action_required` ⇒ có action.
- `late_delivery_seller` ⇒ `late_seller_ids` khác rỗng và responsible party là các seller đó.
- Actions unique, confidence trong `[0, 1]`.

## 7. Reproducibility

- Python 3.11, dependency theo `pyproject.toml`; không có model/LLM, không random seed.
- Concurrency: 1 case tại một thời điểm, call tuần tự trong case.
- Lệnh: `day09 run && day09 validate && day09 package --output dist/submission.zip`.
- Test: `pytest -q` (gateway giả trong `tests/test_workflow.py`).
