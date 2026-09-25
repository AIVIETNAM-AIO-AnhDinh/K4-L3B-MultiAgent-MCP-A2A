"""Offline tests for the coordinator/specialist workflow (no MCP network, no audited calls)."""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from student_agent.agents.ledger import EvidenceLedger
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import EvidenceGateway, MCPToolError, ToolSpec
from student_agent.workflow import solve_case

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = Contracts(ROOT / "contracts" / "schemas")
ORDER_ID = "af0bbb47f125381ce9f3597dc70ef07b"
OTHER_ORDER = "0123456789abcdef0123456789abcdef"
SELLER = "seller-aaaa"
DOMAINS = {
    "get_order": "order", "get_customer_history": "customer", "get_order_items": "item",
    "get_product_context": "product", "get_sellers": "seller", "get_shipment_summary": "shipment",
    "get_order_payments": "payment", "get_payment_timeline": "payment",
    "get_refund_timeline": "refund", "get_policy": "policy",
}
ARGS = {"get_customer_history": "customer_unique_id", "get_policy": "policy_version"}


def envelope(tool: str, data: Any, n: int) -> dict[str, Any]:
    return {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": f"ev_{tool}_{n:04d}_abcdefghijklmnop",
        "result_hash": f"sha256:{'a' * 64}",
        "domain": DOMAINS[tool],
        "data": data,
        "warnings": [],
    }


def base_world() -> dict[str, Any]:
    """Olist-shaped evidence for one order; tests mutate copies of it."""
    return {
        "orders": {
            ORDER_ID: {
                "order_id": ORDER_ID, "customer_unique_id": "cust-real-1",
                "order_status": "delivered",
                "order_purchase_timestamp": "2017-12-10T09:00:00-03:00",
                "order_delivered_carrier_date": "2017-12-12T09:00:00-03:00",
                "order_delivered_customer_date": "2017-12-28T09:00:00-03:00",
                "order_estimated_delivery_date": "2017-12-22T00:00:00-03:00",
            }
        },
        "history": {
            "customer_unique_id": "cust-real-1",
            "orders": [
                {"order_id": ORDER_ID, "order_status": "delivered",
                 "order_purchase_timestamp": "2017-12-10T09:00:00-03:00"},
                {"order_id": OTHER_ORDER, "order_status": "delivered",
                 "order_purchase_timestamp": "2016-10-01T09:00:00-03:00"},
            ],
        },
        "items": [
            {"order_id": ORDER_ID, "order_item_id": 1, "product_id": "prod-1",
             "seller_id": SELLER, "shipping_limit_date": "2017-12-14T09:00:00-03:00",
             "price": 79.0, "freight_value": 18.0},
        ],
        "shipment": {
            "order_id": ORDER_ID, "shipment_id": "shp-1",
            "delivered_carrier_at": "2017-12-12T09:00:00-03:00",
            "delivered_customer_at": "2017-12-28T09:00:00-03:00",
            "estimated_delivery_at": "2017-12-22T00:00:00-03:00",
            "events": [],
        },
        "payments": [
            {"order_id": ORDER_ID, "payment_sequential": 1, "payment_type": "credit_card",
             "payment_value": 97.0},
        ],
        "timeline": {"events": [
            {"event_type": "payment_captured", "amount_brl": 97.0, "status": "confirmed",
             "event_at": "2017-12-10T10:00:00-03:00", "payment_reference": "pay-1"},
            # decoys: other order, and a capture far outside the case window
            {"order_id": OTHER_ORDER, "event_type": "payment_captured", "amount_brl": 500.0,
             "event_at": "2017-12-11T10:00:00-03:00"},
            {"event_type": "payment_captured", "amount_brl": 300.0,
             "event_at": "2016-01-01T10:00:00-03:00", "payment_reference": "pay-old"},
        ]},
        "refunds": {"events": []},
        "policy": {"policy_version": "EC_POLICY_V2", "rules": [
            {"issue": "late_delivery_logistics", "case_status": "action_required",
             "recommended_action": "refund_freight", "refund_basis": "freight_value",
             "responsible_parties": ["logistics_provider"]},
            {"issue": "late_delivery_seller", "case_status": "action_required",
             "recommended_action": "refund_freight", "refund_basis": "freight_value",
             "responsible_parties": ["seller"]},
            {"issue": "duplicate_charge", "case_status": "action_required",
             "recommended_action": "refund_duplicate_capture", "refund_basis": "duplicate",
             "responsible_parties": ["payment_provider"]},
            {"issue": "valid_split_payment", "case_status": "no_action",
             "recommended_action": "explain_split_payment", "refund_basis": "none",
             "responsible_parties": []},
            {"issue": "refund_failed", "case_status": "action_required",
             "recommended_action": "retry_refund", "refund_basis": "failed_refund",
             "responsible_parties": ["payment_provider"]},
        ]},
    }


class FakeGateway:
    def __init__(self, world: dict[str, Any], specs: dict[str, ToolSpec] | None = None,
                 failures: dict[str, list[BaseException]] | None = None) -> None:
        self.world = world
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.failures = failures or {}
        self.specs = specs if specs is not None else {
            name: ToolSpec(name, input_schema={
                "type": "object",
                "properties": {"case_id": {}, ARGS.get(name, "order_id"): {}},
                "required": ["case_id", ARGS.get(name, "order_id")],
            }) for name in DOMAINS
        }

    async def list_tools(self) -> list[str]:
        return sorted(self.specs)

    async def describe_tools(self) -> dict[str, ToolSpec]:
        return dict(self.specs)

    async def call(self, tool: str, *, case_id: str, **arguments: Any) -> dict[str, Any]:
        self.calls.append((tool, {"case_id": case_id, **arguments}))
        pending = self.failures.get(tool)
        if pending:
            raise pending.pop(0)
        order_id = arguments.get("order_id") or arguments.get("orderId")
        world = self.world
        if tool == "get_order":
            if order_id not in world["orders"]:
                raise MCPToolError("order not found")
            data: Any = world["orders"][order_id]
        else:
            data = {
                "get_customer_history": world["history"], "get_order_items": world["items"],
                "get_product_context": [{"product_id": "prod-1", "availability": "in_stock"}],
                "get_sellers": [{"seller_id": SELLER}], "get_shipment_summary": world["shipment"],
                "get_order_payments": world["payments"], "get_payment_timeline": world["timeline"],
                "get_refund_timeline": world["refunds"], "get_policy": world["policy"],
            }[tool]
        return envelope(tool, copy.deepcopy(data), len(self.calls))


class TraceSpy:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, **event: Any) -> dict[str, Any]:
        self.events.append(event)
        return event


def make_case(topic: str, *, claimed: str | None = ORDER_ID,
              candidates: list[str] | None = None) -> dict[str, Any]:
    return {
        "case_id": "L3B_CASE_001",
        "opened_at": "2018-01-01T09:00:00-03:00",
        "customer_request": {
            "claimed_order_id": claimed,
            "claims": [{"claim_id": "claim-a", "topic": topic},
                       {"claim_id": "claim-b", "topic": "requested_full_refund"}],
        },
        "policy_version": "EC_POLICY_V2",
        "candidate_order_ids": candidates if candidates is not None else [ORDER_ID, "candidate-1"],
        "investigation_scope": {"include_customer_history": True,
                                "include_product_context": True,
                                "require_independent_verification": True},
        "customer_unique_id_hint": "customer-hint",
    }


def run(case: dict[str, Any], gateway: FakeGateway) -> tuple[dict[str, Any], TraceSpy]:
    trace = TraceSpy()
    output = asyncio.run(solve_case(case, gateway, trace))  # type: ignore[arg-type]
    CONTRACTS.validate_output(output, "test output")
    return output, trace


def tools(gateway: FakeGateway) -> list[str]:
    return [name for name, _ in gateway.calls]


def assert_provenance(output: dict[str, Any], trace: TraceSpy) -> None:
    consumed = {ref for e in trace.events if e["event_type"] == "tool_result_consumed"
                for ref in e["evidence_refs"]}
    assert output["evidence_refs"] and set(output["evidence_refs"]) <= consumed
    for claim in output["claim_assessments"]:
        assert set(claim["evidence_refs"]) <= consumed
    kinds = {e["event_type"] for e in trace.events}
    assert kinds >= {"task_assigned", "handoff", "tool_result_consumed", "policy_decided",
                     "verification_completed"}
    assert all(e["case_id"] == "L3B_CASE_001" for e in trace.events)


# ---- scenarios --------------------------------------------------------------------------
def test_late_logistics_refunds_freight_with_minimal_calls() -> None:
    gateway = FakeGateway(base_world())
    output, trace = run(make_case("late_delivery_logistics"), gateway)

    assert tools(gateway) == ["get_order", "get_customer_history", "get_order_items",
                              "get_shipment_summary", "get_payment_timeline", "get_policy"]
    assert all(args["case_id"] == "L3B_CASE_001" for _, args in gateway.calls)
    assert output["entity_resolution"] == {
        "status": "resolved", "resolved_order_ids": [ORDER_ID],
        "rejected_candidates": ["candidate-1"], "confidence": 0.98,
    }
    assert output["customer_context"] == {
        "customer_unique_id": "cust-real-1", "related_order_ids": [ORDER_ID, OTHER_ORDER],
    }
    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"
    assert output["assessment"]["case_status"] == "action_required"
    assert output["shipment_analysis"]["verdict"] == "logistics_delay"
    # decoy captures (other order / 2016) are excluded from the authoritative total
    assert output["payment_analysis"]["captured_total_brl"] == 97.0
    assert output["financial_resolution"]["recommended_refund_brl"] == 18.0
    assert output["resolution_actions"] == ["refund_freight"]
    verdicts = {c["claim_id"]: c["verdict"] for c in output["claim_assessments"]}
    assert verdicts == {"claim-a": "supported", "claim-b": "partially_supported"}
    assert_provenance(output, trace)


def test_late_seller_names_seller_and_uses_handoff_vs_shipping_limit() -> None:
    world = base_world()
    world["shipment"]["delivered_carrier_at"] = "2017-12-18T09:00:00-03:00"  # after limit
    output, _ = run(make_case("late_delivery_seller"), FakeGateway(world))
    assert output["shipment_analysis"]["verdict"] == "seller_delay"
    assert output["shipment_analysis"]["late_seller_ids"] == [SELLER]
    assert output["root_cause_analysis"]["responsible_parties"] == [
        {"party_type": "seller", "party_id": SELLER}
    ]
    assert output["assessment"]["confidence"] >= 0.9


def test_evidence_mode_switches_contradicted_late_attribution(monkeypatch) -> None:
    monkeypatch.setenv("DAY09_ISSUE_MODE", "evidence")
    output, _ = run(make_case("late_delivery_seller"), FakeGateway(base_world()))
    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"
    monkeypatch.setenv("DAY09_ISSUE_MODE", "claim")
    output, _ = run(make_case("late_delivery_seller"), FakeGateway(base_world()))
    assert output["assessment"]["primary_issue"] == "late_delivery_seller"
    assert output["assessment"]["confidence"] <= 0.6


def test_duplicate_charge_refunds_only_the_extra_capture() -> None:
    world = base_world()
    world["timeline"]["events"].append(
        {"event_type": "payment_captured", "amount_brl": 97.0, "status": "confirmed",
         "event_at": "2017-12-10T10:05:00-03:00", "payment_reference": "pay-1"})
    gateway = FakeGateway(world)
    output, _ = run(make_case("duplicate_charge"), gateway)
    assert "get_order_payments" in tools(gateway)
    assert output["payment_analysis"]["verdict"] == "duplicate_capture"
    assert output["payment_analysis"]["captured_total_brl"] == 194.0
    assert output["financial_resolution"]["recommended_refund_brl"] == 97.0
    assert {c["field"] for c in output["data_conflicts"]} == {"captured_total_brl"}


def test_valid_split_payment_is_no_action() -> None:
    world = base_world()
    world["payments"] = [
        {"order_id": ORDER_ID, "payment_sequential": 1, "payment_type": "credit_card",
         "payment_value": 60.0},
        {"order_id": ORDER_ID, "payment_sequential": 2, "payment_type": "voucher",
         "payment_value": 37.0},
    ]
    world["timeline"]["events"][0]["amount_brl"] = 60.0
    world["timeline"]["events"].append(
        {"event_type": "payment_captured", "amount_brl": 37.0, "status": "confirmed",
         "event_at": "2017-12-10T10:01:00-03:00", "payment_reference": "pay-2"})
    output, _ = run(make_case("valid_split_payment"), FakeGateway(world))
    assert output["payment_analysis"]["verdict"] == "reconciled"
    assert output["assessment"]["case_status"] == "no_action"
    assert output["financial_resolution"] == {
        "currency": "BRL", "recommended_refund_brl": 0.0, "refund_lines": []}


def test_refund_failed_retries_failed_amount_from_refund_timeline() -> None:
    world = base_world()
    world["refunds"] = {"events": [
        {"event_type": "refund_requested", "amount_brl": 97.0, "refund_id": "rf-1",
         "status": "confirmed", "event_at": "2017-12-26T10:00:00-03:00"},
        {"event_type": "refund_failed", "amount_brl": 97.0, "refund_id": "rf-1",
         "event_at": "2017-12-27T10:00:00-03:00"},
    ]}
    gateway = FakeGateway(world)
    output, _ = run(make_case("refund_failed"), gateway)
    assert "get_refund_timeline" in tools(gateway)
    assert output["payment_analysis"]["verdict"] == "refund_failed"
    assert output["payment_analysis"]["refunded_total_brl"] == 0.0
    assert output["financial_resolution"]["recommended_refund_brl"] == 97.0


def test_refund_is_capped_by_authoritative_timeline() -> None:
    world = base_world()
    world["items"][0]["freight_value"] = 500.0  # policy basis larger than what was captured
    output, _ = run(make_case("late_delivery_logistics"), FakeGateway(world))
    assert output["financial_resolution"]["recommended_refund_brl"] == 97.0


def test_unknown_claimed_order_is_not_found_and_stops_domain_calls() -> None:
    gateway = FakeGateway(base_world())
    case = make_case("late_delivery_logistics", claimed="ffffffffffffffffffffffffffffffff",
                     candidates=["ffffffffffffffffffffffffffffffff", "candidate-9"])
    output, _ = run(case, gateway)
    assert output["entity_resolution"]["status"] == "not_found"
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert not {"get_shipment_summary", "get_payment_timeline"} & set(tools(gateway))


def test_candidates_are_narrowed_by_customer_history_before_get_order() -> None:
    gateway = FakeGateway(base_world())
    case = make_case("late_delivery_logistics", claimed=None,
                     candidates=["candidate-1", "ffffffffffffffffffffffffffffffff", ORDER_ID])
    output, _ = run(case, gateway)
    assert tools(gateway).count("get_order") == 1
    assert output["entity_resolution"]["status"] == "resolved"
    assert output["entity_resolution"]["resolved_order_ids"] == [ORDER_ID]


# ---- ledger / gateway mechanics -----------------------------------------------------------
def ledger_for(gateway: FakeGateway) -> EvidenceLedger:
    specs = asyncio.run(gateway.describe_tools())
    return EvidenceLedger("L3B_CASE_001", gateway, TraceSpy(), specs)  # type: ignore[arg-type]


def test_ledger_maps_arguments_from_discovered_schema_and_caches() -> None:
    specs = {"get_order": ToolSpec("get_order", input_schema={
        "properties": {"case_id": {}, "orderId": {}}, "required": ["case_id", "orderId"]})}
    gateway = FakeGateway(base_world(), specs=specs)
    ledger = ledger_for(gateway)
    first = asyncio.run(ledger.call("entity-customer-agent", "get_order", order_id=ORDER_ID))
    second = asyncio.run(ledger.call("entity-customer-agent", "get_order", order_id=ORDER_ID))
    assert first is second and gateway.calls == [
        ("get_order", {"case_id": "L3B_CASE_001", "orderId": ORDER_ID})]


def test_ledger_skips_undiscovered_tools_and_enforces_permissions() -> None:
    gateway = FakeGateway(base_world(), specs={})
    ledger = ledger_for(gateway)
    assert asyncio.run(ledger.call("policy-agent", "get_policy", policy_version="x")) is None
    assert gateway.calls == [] and ledger.failures == {"get_policy": "tool_not_discovered"}
    with pytest.raises(PermissionError):
        asyncio.run(ledger.call("shipment-agent", "get_policy", policy_version="x"))


def test_ledger_retries_transport_once_but_never_business_errors() -> None:
    gateway = FakeGateway(base_world(), failures={
        "get_shipment_summary": [TimeoutError()],
        "get_policy": [MCPToolError("nope")],
    })
    ledger = ledger_for(gateway)
    assert asyncio.run(ledger.call("shipment-agent", "get_shipment_summary", order_id=ORDER_ID))
    assert asyncio.run(ledger.call("policy-agent", "get_policy", policy_version="v")) is None
    assert tools(gateway) == ["get_shipment_summary", "get_shipment_summary", "get_policy"]
    assert ledger.audited_calls == 3


def test_ledger_respects_call_budget() -> None:
    gateway = FakeGateway(base_world())
    ledger = ledger_for(gateway)
    ledger.call_budget = 1
    asyncio.run(ledger.call("shipment-agent", "get_shipment_summary", order_id=ORDER_ID))
    assert asyncio.run(ledger.call("policy-agent", "get_policy", policy_version="v")) is None
    assert ledger.failures["get_policy"] == "call_budget_exhausted"


class ContractSpy:
    def validate_evidence(self, value: Any, label: str) -> None:
        assert value["schema_version"] == "day09-mcp-evidence-v1"
        assert label.startswith("MCP tool")


def test_gateway_supports_mcp_v2_snake_case_result() -> None:
    expected = envelope("get_order", {"order_id": ORDER_ID}, 1)
    session = SimpleNamespace(
        call_tool=lambda *args, **kwargs: None,
    )

    async def call_tool(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(is_error=False, structured_content=expected, content=[])

    session.call_tool = call_tool
    gateway = EvidenceGateway(session, ContractSpy())  # type: ignore[arg-type]
    actual = asyncio.run(gateway.call("get_order", case_id="L3B_CASE_001", order_id=ORDER_ID))
    assert actual == expected


def test_gateway_surfaces_mcp_tool_error_without_parsing_content() -> None:
    async def call_tool(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(
            is_error=True,
            structured_content=None,
            content=[SimpleNamespace(text="not found")],
        )

    session = SimpleNamespace(call_tool=call_tool)
    gateway = EvidenceGateway(session, ContractSpy())  # type: ignore[arg-type]
    with pytest.raises(MCPToolError, match="not found"):
        asyncio.run(gateway.call("get_order", case_id="L3B_CASE_001", order_id="missing"))


def test_every_released_input_yields_schema_valid_output_offline() -> None:
    """Dry run over the real case inputs with the fake gateway (no MCP, no audit)."""
    from student_agent.cases import load_case_set

    try:
        case_set = load_case_set(ROOT)
    except ValueError:
        pytest.skip("L3B inputs not unzipped locally")
    for case_id in case_set.case_ids:
        case = case_set.cases[case_id]
        claimed = case["customer_request"]["claimed_order_id"]
        world = base_world()
        world["orders"] = {claimed: dict(world["orders"][ORDER_ID], order_id=claimed)}
        world["history"]["orders"][0]["order_id"] = claimed
        for record in world["items"] + world["payments"]:
            record["order_id"] = claimed
        gateway = FakeGateway(world)
        trace = TraceSpy()
        output = asyncio.run(solve_case(case, gateway, trace))  # type: ignore[arg-type]
        CONTRACTS.validate_output(output, case_id)
        assert output["case_id"] == case_id
        assert len(gateway.calls) <= 7
        assert output["financial_resolution"]["recommended_refund_brl"] <= 97.0


def test_history_falls_back_to_real_customer_unique_id_once() -> None:
    """Olist: the hint is a server alias; a real customer_unique_id is the one fallback."""
    world = base_world()

    class HintRejectingGateway(FakeGateway):
        async def call(self, tool: str, *, case_id: str, **arguments: Any) -> dict[str, Any]:
            if tool == "get_customer_history" and arguments["customer_unique_id"] != "cust-real-1":
                self.calls.append((tool, {"case_id": case_id, **arguments}))
                raise MCPToolError("unknown customer")
            return await super().call(tool, case_id=case_id, **arguments)

    gateway = HintRejectingGateway(world)
    output, _ = run(make_case("late_delivery_logistics"), gateway)
    history_calls = [args for name, args in gateway.calls if name == "get_customer_history"]
    assert [a["customer_unique_id"] for a in history_calls] == ["customer-hint", "cust-real-1"]
    assert output["customer_context"]["related_order_ids"] == [ORDER_ID, OTHER_ORDER]


def test_olist_naive_timestamps_and_order_record_dates_are_understood() -> None:
    world = base_world()
    world["shipment"] = {"order_id": ORDER_ID}  # summary without dates -> use order record
    world["orders"][ORDER_ID].update({
        "order_delivered_carrier_date": "2017-12-12 09:00:00",
        "order_delivered_customer_date": "2017-12-28 18:30:00",
        "order_estimated_delivery_date": "2017-12-22 00:00:00",
    })
    output, _ = run(make_case("late_delivery_logistics"), FakeGateway(world))
    assert output["shipment_analysis"] == {
        "verdict": "logistics_delay", "late_seller_ids": [], "timeline_complete": True}
