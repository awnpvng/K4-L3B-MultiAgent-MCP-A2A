# K4 L3B — Multi-Agent MCP + A2A

## Mục tiêu

Xây dựng hệ thống multi-agent điều tra khiếu nại thương mại điện tử.

Ngoài kết luận nghiệp vụ, yêu cầu cần phải xử lý xử lý entity resolution, customer context, shipment/payment analysis, source conflict và hiệu quả sử dụng MCP.

## Dữ liệu

Tham khảo dữ liệu tại: https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce

## Quy tắc đặt tên

Làm nhóm hoặc cá nhân, khi fork về các bạn giữ nguyên tên gốc repo, không đổi tên

## 1. Cài đặt

Yêu cầu Python 3.11 trở lên.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
```

Kiểm tra:

```bash
pytest -q
day09 --help
```

## 2. Đăng ký team

1. Mở `/register` trên Competition Workspace.
2. Điền tên team, mã học viên và các thành viên.
3. Nhập registration code của lớp.
4. Lưu Team API Key dạng `sk-team-...` được hiển thị sau khi đăng ký.

Điền thông tin thật vào `.env`:

```dotenv
COMPETITION_API_URL=http://127.0.0.1:8081
COMPETITION_TEAM_API_KEY=sk-team-your_key
MCP_ENDPOINT=http://127.0.0.1:8001/mcp
```

## 3. Tải input

Tải ZIP input **L3B** từ GitHub Release và giải nén vào root repo:

```bash
unzip l3b-inputs-<version>.zip -d .
day09 validate-inputs
```

Cấu trúc đúng:

```text
case-set.json
inputs/
├── L3B_CASE_001.json
├── ...
└── L3B_CASE_100.json
```

Một số case không cung cấp exact order ID. Agent phải dùng candidate và evidence để resolve entity.

## 4. Sử dụng MCP

MCP Gateway cung cấp evidence về order, customer, product, shipment, payment, refund và policy. Mọi call đều được server audit theo team và case.

Xem các tool hiện có:

```bash
day09 mcp-tools
```

Ví dụ gọi tool trong `workflow.py`:

```python
evidence = await gateway.call(
    "get_customer_history",
    case_id=case["case_id"],
    customer_unique_id=customer_unique_id,
)

evidence_ref = evidence["evidence_ref"]
customer_data = evidence["data"]
```

Khi dùng evidence, ghi lại trong trace:

```python
trace.emit(
    case_id=case["case_id"],
    event_type="tool_result_consumed",
    actor="entity-agent",
    tool_name="get_customer_history",
    evidence_refs=[evidence_ref],
)
```

Quy tắc quan trọng:

- luôn truyền đúng `case_id`;
- dùng tool discovery, không đoán tên tool;
- không sửa hoặc tự tạo `evidence_ref`;
- không dùng evidence chéo case;
- giới hạn retry, cache trong phạm vi case và tránh gọi tool thừa.

Tất cả MCP calls đều được audit và có thể ảnh hưởng điểm efficiency, kể cả call không được đưa vào output.

## 5. Xây dựng multi-agent workflow

Triển khai tại:

```text
src/student_agent/workflow.py
```



### Kiến trúc kỳ vọng:

```
                          ┌──────────────────────────┐
                          │   Coordinator / Router   │
                          └─────────────┬────────────┘
                                        │ (Handoff)
         ┌──────────────────────────────┼──────────────────────────────┐
         ▼                              ▼                              ▼
┌──────────────────┐           ┌──────────────────┐           ┌──────────────────┐
│ Order/Item Agent │           │  Payment Agent   │           │  Shipment Agent  │
└────────┬─────────┘           └────────┬─────────┘           └────────┬─────────┘
         │                              │                              │
         └──────────────────────────────┼──────────────────────────────┘
                                        │ (MCP Evidence Collector)
                                        ▼
                               ┌──────────────────┐
                               │   Policy Agent   │
                               └────────┬─────────┘
                                        │
                                        ▼
                               ┌──────────────────┐
                               │  Verifier Agent  │
                               └────────┬─────────┘
                                        │ (Validated Output)
                                        ▼
                                   [END OUTPUT]
```


Hàm chính:

```python
async def solve_case(case, gateway, trace) -> dict:
    ...
```

Có thể tổ chức các vai trò:

- entity/customer agent;
- coordinator;
- order/product agent;
- shipment agent;
- payment/refund agent;
- policy hoặc conflict agent;
- verifier.

Competition không chấm tên framework hay số lượng class. Scorer đánh giá output, evidence, efficiency và sự phối hợp thể hiện trong trace.

Trace chỉ ghi sự kiện quan sát được như `task_assigned`, `handoff`, `tool_result_consumed`, `verification_completed`.

Hoàn thiện mô tả thiết kế trong `ARCHITECTURE.md`.

bonus từ btc:
""

Các nhiệm vụ trọng tâm:

1. Khóa Public Contracts (contracts/schemas/):
   l3a-output-v2.schema.json (hoặc l3b): Schema bắt buộc cho output từng case.
   trace-event-v1.schema.json: Schema cho trace log observable.
   submission-manifest-v2.schema.json: Schema cho manifest khi đóng gói nộp bài.
   mcp-evidence-response-v1.schema.json: Cấu trúc phong bì (envelope) trả về từ MCP Gateway.
   Nguyên tắc: Không thêm bất kỳ field nào ngoài schema. Nếu có sai lệch, JSON Schema luôn là chân lý ưu tiên tối cao, tuyệt đối tuân thủ chỗ này nhé các bạn, ràng buộc đau đớn mà liêm
2. Linh hoạt framework các bạn là kỹ sư thiết kế thực chiến nên là thoải mái sáng tạo:
   Điểm triển khai chính nằm tại: src/student_agent/workflow.py:
   async def solve_case(case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter) -> dict[str, Any]:# Triển khai coordinator và specialist agents tại đây

   ...
   Chép
   Cuộc thi không chấm điểm dựa trên tên framework (học viên có thể dùng LangGraph, Semantic Kernel, CrewAI hoặc thuần Python async state-machine). Hệ thống chỉ đánh giá kết quả nghiệp vụ, tính hợp lệ của bằng chứng MCP và trace log.
3. Hoàn thiện mô tả kiến trúc trong ARCHITECTURE.md:
   Phác thảo luồng handoff, tool permissions của từng agent, cơ chế retry khi MCP gặp sự cố.

"

"

Mục tiêu:
Từng agent chuyên trách truy vấn bằng chứng có thẩm quyền qua MCP Evidence Gateway theo đúng scope của từng case và ghi nhận trace audit.

Nguyên tắc của MCP Gateway:

# Nguyên tắc	Nếu vi phạm

1	Truyền đúng case_id cho mọi MCP call	Bị từ chối truy cập (403 Forbidden)
2	KHÔNG tự sinh hoặc sửa đổi evidence_ref	Hard Gate 0 điểm toàn bài
3	Chỉ trích dẫn evidence thực sự hỗ trợ kết luận	Bị trừ điểm thành phần Evidence Relevance
4	Ghi nhận event tool_result_consumed trong trace	Không được công nhận tính xác thực
5	Server lưu Audit độc lập (Hash, Latency, Status)	Bị phát hiện nếu giả mạo trace client
Đọc kĩ chỗ này để tránh được các lỗi nhé mọi người.

Triển khai cụ thể:

1. Gọi Tool qua Gateway:

# Lấy dữ liệu đơn hàng có thẩm quyền từ MCP

evidence = await gateway.call(
    "get_order",
    case_id=case["case_id"],
    order_id=order_id,
)
evidence_ref = evidence["evidence_ref"]
order_data = evidence["data"]
Chép

1. Ghi nhận Trace Event hợp lệ:

# Ghi nhận sự kiện tiêu thụ bằng chứng vào trace audit

trace.emit(
    case_id=case["case_id"],
    event_type="tool_result_consumed",
    actor="order-agent",
    tool_name="get_order",
    evidence_refs=[evidence_ref],
)



"

## 6. Chạy và kiểm tra

```bash
day09 run
day09 validate
```

Kết quả được tạo tại:

```text
outputs/<case_id>.json
traces/trace.jsonl
```

Nếu output pass schema nhưng điểm thấp, cần kiểm tra semantic, entity resolution, evidence, consistency, confidence, workflow và số MCP calls.

## 7. Đóng gói và nộp bài

```bash
day09 package --output dist/submission.zip
```

ZIP chỉ được chứa:

```text
manifest.json
trace.jsonl
outputs/<case_id>.json
```

Không đưa source, input, `.env`, API key hoặc debug log vào ZIP. Sau đó upload `dist/submission.zip` tại workspace `/l3b` và chọn submission muốn dùng làm final.

## Tiêu chí chấm điểm công khai

| Thành phần                                         | Trọng số |
| ---------------------------------------------------- | ---------: |
| Độ đúng nghiệp vụ (`semantic`)               |        40% |
| Chất lượng bằng chứng (`evidence`)            |        15% |
| Evidence đúng MCP audit (`provenance`)           |        15% |
| Tính nhất quán giữa các field (`consistency`) |        10% |
| Đúng JSON Schema (`schema`)                      |         5% |
| Confidence hợp lý (`calibration`)                |         5% |
| Quy trình multi-agent trong trace (`workflow`)    |         5% |
| Hiệu quả gọi tool (`efficiency`)                |         5% |

Case có thể nhận 0 điểm nếu:

- sai `case_id` hoặc output không thể chấm theo schema;
- thiếu evidence bắt buộc;
- evidence ref không tồn tại;
- evidence thuộc team, run hoặc case khác.
