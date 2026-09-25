# L3B Architecture Record

Tài liệu này mô tả các quyết định có thể kiểm chứng của implementation trong
`src/student_agent/workflow.py`. Trace chỉ chứa sự kiện observable; không ghi prompt,
chain-of-thought, API key hay payload nhạy cảm.

## 1. System overview

```text
                             ┌──────────────────────┐
Input ──────────────────────▶│ Coordinator / Router │
                             └──────────┬───────────┘
                                        │ task_assigned
                 ┌──────────────────────┼──────────────────────┐
                 ▼                      ▼                      ▼
       Entity/Customer Agent    Order/Item Agent        Shipment Agent
                 │                      │                      │
                 └──────────────┬───────┴──────────────┬───────┘
                                ▼                      ▼
                     Payment/Refund Agent        Policy Agent
                                │                      │
                                └──────────┬───────────┘
                                           ▼
                                Conflict Resolver
                                           │
                                           ▼
                                    Verifier Agent
                                           │ validated output
                                           ▼
                                         END

Mỗi tool call đi qua case-scoped `EvidenceLedger`: discovery allow-list → cache →
timeout/retry → schema validation tại `EvidenceGateway` → `tool_result_consumed`.
Coordinator là agent duy nhất xây output; specialist chỉ trả facts và evidence refs.
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity/customer | case, claimed ID, candidates, customer hint | Reject marker/invalid candidate, resolve order qua MCP, lấy customer history | `get_order`, `get_customer_history` | resolution status, selected order, related orders |
| Coordinator | case và specialist results | Route bounded DAG, tổng hợp output, không tự tạo evidence | Không gọi domain tool trực tiếp | assignments, handoffs, draft output |
| Order/item | resolved order | Thu item/product/seller IDs và context | `get_order_items`, `get_product_context`, `get_sellers` | entity facts + refs |
| Shipment | resolved order | Phân loại timeline, seller/logistics delay | `get_shipment_summary` | shipment verdict + refs |
| Payment/refund | resolved order, issue | Đối soát capture/refund theo case time window | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | totals/verdict + refs |
| Policy | policy version, classified issue | Chọn published rule cho status, action, refund, responsibility | `get_policy` | policy decision + ref |
| Conflict resolver | facts, `opened_at`, source refs | Chọn record gần case time; event ưu tiên snapshot | Không gọi tool | `data_conflicts`, selected facts |
| Verifier | complete draft + ledger | Check provenance, totals, entity/status/confidence invariants | Không gọi tool | `VERIFIED` hoặc fail closed |

Tool discovery tạo allow-list nhưng không mở rộng permission trong bảng. Ledger nhận actor
ở mỗi call để permission và provenance có thể audit từ source/trace.

## 3. Entity resolution và A2A protocol

Logical A2A envelope (nội bộ, không serialize vào trace):

```json
{
  "protocol": "day09-a2a-v1",
  "case_id": "L3B_CASE_001",
  "sender": "coordinator",
  "recipient": "shipment-agent",
  "task": "INVESTIGATE_SHIPMENT",
  "hop": 1,
  "payload": {"order_id": "..."},
  "evidence_refs": []
}
```

`case_id` là correlation/scope key bắt buộc. Workflow là DAG một chiều; specialist chỉ
handoff về coordinator, không gọi lẫn nhau, nên không có vòng lặp. Mỗi task chạy tối đa
một lần cho một `(tool, arguments)` nhờ cache. Claimed ID được ưu tiên; marker
`candidate-*` và candidate sai định dạng không phải claimed ID bị reject tại chỗ để tránh
call audit vô ích. Candidate opaque còn lại phải được `get_order` xác nhận. Một match là
`resolved`, nhiều match là `ambiguous`, không match là `not_found`; chỉ entity resolved
mới mở các domain investigation.

## 4. Evidence và conflict lifecycle

1. Gateway nhận envelope và validate bằng `mcp-evidence-response-v1.schema.json`.
2. Ledger chỉ sống trong một `solve_case`; cache key gồm tool + arguments, `case_id` được
   truyền bắt buộc, nên evidence không thể tái sử dụng chéo case.
3. Sau response hợp lệ, ledger lưu nguyên `evidence_ref` rồi emit
   `tool_result_consumed`. Ref không bị sửa, hash lại hoặc tự sinh.
4. Output-level refs là tập con duy nhất của ledger. Claim-level refs chỉ lấy domain liên
   quan cộng policy.
5. Với snapshot trùng/mâu thuẫn, record có business timestamp gần `opened_at` được chọn;
   authoritative timeline event ưu tiên summary snapshot. Payment/refund chỉ cộng event
   trong cửa sổ ±120 ngày quanh case để loại decoy lịch sử.
6. Mỗi conflict observable được ghi vào `data_conflicts` với hai source, selected source
   và resolution code. Nếu thiếu nguồn đủ mạnh thì verdict chuyển
   `insufficient_evidence`, không nội suy dữ liệu.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout/transport | 1 retry, 45 s/attempt | Mark tool unavailable for case, lower confidence | verifier attributes `mcp_failure_count` |
| MCP tool rejection/not found | 0 retry | Reject entity or use insufficient evidence | `ENTITY_NOT_FOUND` / policy unavailable |
| Entity ambiguous | 0 broad scan | Preserve all matched/rejected IDs, cap confidence 0.55 | `ENTITY_AMBIGUOUS` |
| Source conflict | 0 extra query | Apply time/event precedence, expose conflict | `AUTHORITATIVE_EVENT` / `CASE_TIME_PROXIMITY` |
| Invalid envelope/specialist result | 0 retry | Fail/degrade; never fabricate evidence | `VERIFICATION_FAILED` |

Efficiency controls: maximum five candidates; negative marker rejected locally; discovery
cached per gateway; each exact call cached per case; base investigation calls each required
tool once; refund timeline chỉ gọi cho refund issue. Retry chỉ áp dụng transport timeout và
giữ nguyên idempotent arguments. MCP business error không retry.

## 6. Verification invariants

Trước finalize, verifier kiểm tra:

- output `case_id` và entity IDs thuộc case hiện tại; resolved phải có affected order;
- mọi output ref thuộc ledger của case, không trùng; claim refs là domain-relevant;
- tổng `refund_lines` bằng `recommended_refund_brl`, amount không âm;
- `no_action` không đi kèm refund dương;
- capture/refund lấy từ timeline window, policy quyết định refundable amount;
- conflict có source precedence tường minh; seller responsibility bổ sung seller ID;
- confidence nằm `[0, 1]` và bị cap khi entity ambiguous/missing tool;
- output được CLI validate lần cuối bằng `l3b-output-v2.schema.json` trước atomic write.

Verifier fail thì raise trước khi output được ghi. Trace `verification_completed` luôn chứa
decision code và chỉ tối đa 20 evidence refs theo trace schema.

## 7. Reproducibility

- Runtime: Python 3.11+, dependency ranges được khóa trong `pyproject.toml`.
- Framework: thuần Python async state machine; không model/LLM call và không randomness
  nghiệp vụ. Chỉ `event_id` và timestamps của trace là nondeterministic.
- Concurrency: một case và một MCP call tại một thời điểm để trace/order/audit ổn định.
- Limits: 45 s mỗi attempt, tối đa hai attempts cho transient failure, năm candidates,
  30 output refs, 20 trace refs.
- Contract source of truth: bốn schema versioned trong `contracts/schemas/`; breaking
  change phải tạo version mới, không đổi semantics file V1/V2 hiện tại.
- Commands: `pytest -q`, `day09 run`, `day09 validate`,
  `day09 package --output dist/submission.zip`.
- Secrets chỉ đọc từ `.env`; không đi vào output, trace, manifest hay tài liệu này.
