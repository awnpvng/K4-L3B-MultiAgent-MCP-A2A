# L3B Architecture Record

Triển khai: `src/student_agent/workflow.py` — thuần Python async state-machine, không dùng LLM, deterministic.

## 1. System overview

```text
case input
   │ case_received (cli)
   ▼
Coordinator ──task_assigned──► Entity agent ──(get_customer_history)──► resolve order + chọn record hợp lệ
   │                                   └─handoff──► Coordinator
   ├─task_assigned──► Order agent    (get_order, get_order_items, get_product_context) ─handoff─► Shipment agent
   ├─task_assigned──► Shipment agent (get_shipment_summary)                           ─handoff─► Policy agent
   ├─task_assigned──► Payment agent  (get_payment_timeline, get_refund_timeline)      ─handoff─► Policy agent
   ├─task_assigned──► Policy agent   (get_policy) ── policy_decided ─handoff─► Verifier
   └─task_assigned──► Verifier (không gọi tool) ── verification_completed ─handoff─► Coordinator
                                                             │
                                                  output JSON + case_finalized (cli)
```

Mọi MCP result thành công được ghi `tool_result_consumed` với đúng `evidence_ref` server trả về.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity/customer | case (candidates, customer hint, opened_at) | Resolve order từ candidate bằng customer history; reject candidate không thuộc khách; chọn record hợp lệ theo thời điểm mở case | `get_customer_history` | entity status, order_id, record hợp lệ → Coordinator |
| Coordinator | case + kết quả entity | Điều phối, giao task, gom kết quả | không gọi tool | task_assigned cho từng specialist |
| Order/product | order_id, record hợp lệ | Lấy order row, items/seller trong đúng time window, tổng tiền kỳ vọng, product context | `get_order`, `get_order_items`, `get_product_context` | items, expected_total, conflict flags → Shipment |
| Shipment | record hợp lệ + items | So carrier vs shipping_limit, delivered vs estimated, event `delivered_late` | `get_shipment_summary` | verdict, late_seller_ids → Policy |
| Payment/refund | order_id, expected_total | Tổng captured, duplicate/split/mismatch, trạng thái refund | `get_payment_timeline`, `get_refund_timeline` | payment facts → Policy |
| Policy | facts của specialists | Phân loại primary_issue, áp rule của `EC_POLICY_V2` (status, action, refund, bên chịu trách nhiệm) | `get_policy` | policy_decided → Verifier |
| Conflict resolver | (gộp trong Order/Shipment/Verifier) | Phát hiện record trùng lặp khác thời điểm, chọn source theo scope thời gian | không gọi tool | `data_conflicts` |
| Verifier | toàn bộ facts + decision | Kiểm tra invariant, hiệu chỉnh confidence, chỉ trích evidence liên quan | không gọi tool | verification_completed → Coordinator |

Least privilege được thực thi bằng `TOOL_PERMISSIONS`: gọi tool ngoài quyền → `PermissionError`.

## 3. Entity resolution và A2A protocol

- Candidate được chấp nhận khi xuất hiện trong `get_customer_history` của `customer_unique_id_hint`. Candidate không có trong history (vd. `candidate-NNN`) bị reject **mà không gọi MCP** → tiết kiệm call.
- Một order có nhiều record ở các thời điểm khác nhau. Record hợp lệ = record có `order_purchase_timestamp` muộn nhất nhưng **≤ `opened_at`** của case. Các row khác (items, payment events, refund events, shipment events) được gán vào record theo time window `[purchase, purchase của record kế tiếp)`.
- Confidence entity: 0.95 khi resolve qua history, 0.6 khi chỉ dựa claimed id, 0.4 ambiguous, 0.2 not_found.
- Message envelope = trace event `handoff` (actor → target, `decision_code`, `evidence_refs`). Correlation theo `case_id`; mọi state nằm trong `CaseContext` của một case nên không thể trộn evidence giữa các case. Luồng là DAG cố định, không có vòng lặp.

## 4. Evidence và conflict lifecycle

- `EvidenceGateway.call` validate envelope theo `mcp-evidence-response-v1`. `evidence_ref` được lưu nguyên văn, không bao giờ sinh/sửa.
- Output chỉ trích evidence hỗ trợ kết luận: customer history + tool đặc thù cho primary issue (`ISSUE_TOOLS`) + policy (+ product context khi scope yêu cầu).
- Conflict: khi `get_order`/`get_order_items`/`get_shipment_summary` trả dữ liệu thuộc record khác record hợp lệ → ghi `data_conflicts` với `selected_source=get_customer_history`, `resolution_code=CASE_OPENED_AT_SCOPE`.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout / lỗi tạm thời | 2 retry, backoff 2s, 4s | Bỏ evidence đó, giảm confidence 0.2 | không emit tool_result_consumed |
| Tool "không có dữ liệu" (`get_refund_timeline`) | 1 retry | Coi như không có refund event | — |
| Entity not found/ambiguous | 0 | `insufficient_evidence`, `needs_investigation`, confidence thấp | handoff `ENTITY_NOT_FOUND`/`ENTITY_AMBIGUOUS` |
| Source conflict | 0 | Chọn record theo scope thời gian | `data_conflicts` + verifier attributes |
| Invalid specialist result / không có rule policy | 0 | `escalate_manual_review`, party `unknown` | verification `PASS_WITH_ADJUSTMENTS` |

Budget: tối đa 8 call/case (customer history, order, items, product, shipment, payment timeline, refund timeline, policy). Cache theo case (một tool chỉ gọi một lần mỗi case). Không gọi `get_order_payments`, `get_sellers` vì dữ liệu đã có trong payment timeline / items.

## 6. Verification invariants

- Output schema validate trước khi ghi (cli).
- Entity: đúng một order resolved, resolved ∩ rejected = ∅.
- Evidence ownership: mọi ref trong output thuộc evidence đã nhận trong case này.
- Status/refund: refund > 0 ⇒ không được `no_action`.
- Seller responsibility: `late_delivery_seller` ⇒ party là seller với seller_id thật của order (thay id minh họa trong policy).
- Refund ≤ captured (trừ trường hợp policy quy định).
- Confidence ∈ [0.05, 0.95]; giảm khi có conflict, lỗi MCP, hoặc kết luận khác claim.

## 7. Reproducibility

- Không dùng model/LLM, không random; kết quả deterministic với cùng MCP data.
- Python ≥ 3.11, dependency theo `pyproject.toml`.
- Concurrency: xử lý tuần tự từng case, các call trong case tuần tự (tránh rate limit gateway).
- Lệnh: `day09 run && day09 validate && day09 package --output dist/submission.zip`.
