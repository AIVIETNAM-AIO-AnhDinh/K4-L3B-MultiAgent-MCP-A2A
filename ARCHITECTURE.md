# L3B Architecture Record

Tài liệu này mô tả các quyết định có thể kiểm chứng của implementation trong
`src/student_agent/agents/` (entry point `workflow.solve_case`). Trace chỉ chứa sự kiện
observable; không ghi prompt, chain-of-thought, API key hay payload nhạy cảm.

## 1. System overview

```text
Input ─▶ Coordinator ──task_assigned──▶ Entity/Customer Agent ──handoff──▶ Coordinator
              │                          (get_order, get_customer_history)
              │  plan theo claimed issue (bounded DAG, ≤ 8 hops)
              ├──▶ Order/Item Agent      (get_order_items [+ product/seller khi cần])
              ├──▶ Shipment Agent        (get_shipment_summary)
              ├──▶ Payment/Refund Agent  (get_payment_timeline [+ payments/refund khi cần])
              │        ▼
              │   Verifier: assess_issue (claim vs evidence, không gọi tool)
              ├──▶ Policy Agent ─policy_decided─▶ (get_policy → rule + refund basis)
              ▼
         build draft ──task_assigned──▶ Verifier Agent ──verification_completed──▶ output
```

Mọi tool call đi qua `EvidenceLedger` của đúng case: permission → discovered schema
(argument mapping) → cache → budget → timeout/retry → envelope schema validation tại
`EvidenceGateway` → `tool_result_consumed`. Coordinator là thành phần duy nhất build
output và không tự gọi domain tool; specialist chỉ trả facts, evidence refs và conflicts.

Source layout:

| File | Vai trò |
| --- | --- |
| `mcp_gateway.py` | MCP session, `ToolSpec` discovery (name/description/input/output schema), envelope validation |
| `agents/ledger.py` | case-scoped cache, permission, arg mapping, budget, retry, provenance |
| `agents/a2a.py` | `A2ATask` / `A2AResult` envelope (`day09-a2a-v1`) |
| `agents/entity.py`, `order_item.py`, `shipment.py`, `payment.py`, `policy.py` | specialists |
| `agents/verifier.py` | `assess_issue` (independent claim check) + `verify` (invariants/repairs) |
| `agents/coordinator.py` | planning, dispatch, merge conflicts, build output |
| `agents/parsing.py` | defensive readers (Olist column names + synonyms, time window) |

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Coordinator | case | Plan theo claimed issue, dispatch A2A, merge, build output | Không | draft output |
| Entity/customer | claimed ID, candidates, hint, `opened_at` | Reject marker local, xác nhận order qua MCP, narrow candidate bằng customer history, chọn snapshot gần case time | `get_order`, `get_customer_history` | status, selected order, related orders, conflicts |
| Order/item | order ID, nhu cầu | Item/seller/product IDs, price/freight totals, shipping limit | `get_order_items`, `get_sellers`, `get_product_context` | item facts |
| Shipment | order, shipping limits | Late? (delivered vs estimated), attribution (event actor > carrier handoff vs shipping limit), lost/returned | `get_shipment_summary` | verdict, attribution, conflicts |
| Payment/refund | order, expected total | Capture từ timeline (authoritative, lọc decoy), snapshot đối chiếu, duplicate/mismatch, trạng thái refund cuối cùng | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | totals, verdict, conflicts |
| Policy | issue, facts | Tìm rule của issue trong published policy, suy ra status/actions/basis/parties; default chỉ lấp field thiếu (ghi `fallback_fields`) | `get_policy` | decision + refund amount |
| Verifier | draft + ledger + facts | Claim support, provenance, money cap, seller IDs, consistency | Không | repairs / degrade |

Discovery chỉ có thể *thu hẹp* permission (tool không có trên server → không gọi), không
bao giờ mở rộng. Ledger raise `PermissionError` nếu actor gọi tool ngoài bảng.

## 3. Entity resolution và A2A protocol

Envelope nội bộ (không serialize vào trace):

```json
{"protocol": "day09-a2a-v1", "case_id": "L3B_CASE_001", "sender": "coordinator",
 "recipient": "shipment-agent", "task": "INVESTIGATE_SHIPMENT", "hop": 3,
 "payload": {"order_id": "..."}}
```

`case_id` là correlation key; coordinator từ chối `A2AResult` có `case_id` khác. DAG một
chiều, specialist chỉ handoff về coordinator, giới hạn 8 hop.

Entity resolution (tối thiểu call):

1. Candidate = claimed + `candidate_order_ids`, dedupe, tối đa 5. Marker `candidate-*` và
   ID sai định dạng (không phải claimed) bị reject local, không tốn call.
2. `get_order(claimed)`. `customer_unique_id` lấy từ order record (authoritative) trước hint.
3. `get_customer_history` một lần. Candidate còn lại được lọc bằng history **miễn phí**;
   chỉ candidate có trong history mới được `get_order`. Không có history → tối đa 3 lookup.
4. Một match → `resolved`; nhiều → `ambiguous` (chọn theo case-time proximity, confidence
   ≤ 0.55); không match → `not_found` → `insufficient_evidence`, dừng domain investigation.

## 4. Investigation plan và evidence lifecycle

| Claimed issue | Tool calls (ngoài order + history + policy) |
| --- | --- |
| late_delivery_seller / logistics | items, shipment, payment_timeline |
| canceled_order_paid, unsupported_claim | shipment, payment_timeline |
| unavailable_order_paid | items (+ product_context), shipment, payment_timeline |
| valid_split_payment, payment_mismatch | items, shipment, payment_timeline, order_payments |
| duplicate_charge | shipment, payment_timeline, order_payments |
| refund_pending / refund_failed | shipment, payment_timeline, refund_timeline |

→ 5–7 audited calls/case (starter cũ: 9–10). `get_sellers` chỉ gọi khi item thiếu seller_id.

1. Gateway validate envelope bằng `mcp-evidence-response-v1.schema.json`.
2. Ledger sống trong một `solve_case`; cache key = tool + argument đã map theo schema.
3. `evidence_ref` lưu nguyên văn, emit `tool_result_consumed` (actor = specialist gọi).
4. Output refs ⊆ ledger; verifier loại mọi ref ngoài ledger. Chỉ gọi tool sẽ dùng, nên mọi
   ref trong output đều là evidence đã dùng cho kết luận.
5. Time window của event: `[purchase − 3 ngày, opened_at + 120 ngày]`; event mang `order_id`
   khác bị loại (decoy). Timeline event ưu tiên hơn snapshot.
6. Conflict được ghi vào `data_conflicts` (≤ 5, dedupe theo field):

| Field | Sources | Selected | Code |
| --- | --- | --- | --- |
| `captured_total_brl` | order_payments vs payment_timeline | timeline | `AUTHORITATIVE_EVENT` |
| `delivery_timeliness` | shipment summary vs events | events | `AUTHORITATIVE_EVENT` |
| `delay_responsibility` | event actor vs handoff/limit | events | `AUTHORITATIVE_EVENT` |
| `order_snapshot` | nhiều snapshot trong history | gần case time | `CASE_TIME_PROXIMITY` |
| `order_status`, `customer_order_link` | get_order vs history | get_order | `ORDER_SYSTEM_OF_RECORD` |

## 5. Issue decision, policy và money

- Hypothesis = claim topic hợp lệ đầu tiên. `assess_issue` kiểm tra độc lập bằng evidence:
  `supported` / `contradicted` (+ issue thay thế) / `unknown`.
- `DAY09_ISSUE_MODE=claim` (mặc định): giữ topic trong claim, confidence 0.93 / 0.82 / 0.6.
  `DAY09_ISSUE_MODE=evidence`: chế độ A/B đổi issue khi bị contradicted; issue thay thế nhận confidence 0.72.
  Hai mode để A/B trên public leaderboard.
- Refund basis từ policy (`freight`, `full`, `duplicate`, `difference`, `failed_refund`,
  `none`, số cố định, ratio). Verifier cap refund ≤ captured − refunded theo timeline;
  không có timeline capture → refund 0 ("chỉ refund khi timeline authoritative hỗ trợ").
- `no_action` ⇒ refund 0; seller responsibility ⇒ mỗi seller có `party_id`.

## 6. Failure and efficiency policy

| Failure | Retry | Fallback | Observable |
| --- | ---: | --- | --- |
| Timeout/transport | 1 (45 s/attempt) | tool unavailable cho case, confidence −0.05 | `mcp_failure_count` |
| MCP business error / not found | 0 | reject entity / insufficient evidence | `ENTITY_NOT_FOUND` |
| Tool không có trong discovery | 0 call | bỏ qua | `tool_not_discovered` |
| Argument không map được theo schema | 0 call | bỏ qua | `argument_not_in_schema` |
| Vượt call budget (`DAY09_CALL_BUDGET`, mặc định 10) | 0 call | degrade | `call_budget_exhausted` |
| Verifier lỗi không repair được | — | `needs_investigation`, confidence ≤ 0.3 (vẫn ghi output) | `VERIFICATION_DEGRADED` |

## 7. Verification invariants

- output `case_id` đúng case; resolved phải có affected order;
- mọi ref (output + claim) thuộc ledger case, không trùng, ≤ 30;
- tổng `refund_lines` = `recommended_refund_brl`; không âm; ≤ refundable theo timeline;
- `no_action` không đi kèm refund; actions không trùng;
- confidence ∈ [0, 1], cap khi ambiguous / MCP failure;
- CLI validate output bằng `l3b-output-v2.schema.json` trước atomic write.

## 8. Reproducibility & debug

- Python 3.11+, thuần async state machine, không LLM, không randomness nghiệp vụ.
- Một case, một MCP call tại một thời điểm → trace/audit ổn định.
- `day09 mcp-tools --schema`: in description + input/output schema (discovery, không audit per-case).
- `day09 run --case L3B_CASE_001 --dump-evidence`: debug, lưu envelope thô vào `debug/evidence/`
  (git-ignored, không vào ZIP). Lưu ý: call debug vẫn bị audit.
- Input có thể ở root hoặc `inputs/<release>/` (tự dò `case-set.json`).
- Commands: `pytest -q`, `day09 run`, `day09 validate`, `day09 package --output dist/submission.zip`.
