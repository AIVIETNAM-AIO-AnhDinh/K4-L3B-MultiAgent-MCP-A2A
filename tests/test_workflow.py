from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from student_agent.contracts import Contracts
from student_agent.mcp_gateway import EvidenceGateway, MCPToolError
from student_agent.workflow import solve_case

TOOLS = {
    "get_customer_history",
    "get_order",
    "get_order_items",
    "get_order_payments",
    "get_payment_timeline",
    "get_policy",
    "get_product_context",
    "get_refund_timeline",
    "get_sellers",
    "get_shipment_summary",
}
ORDER_ID = "af0bbb47f125381ce9f3597dc70ef07b"


def evidence(tool_name: str, domain: str, data: Any) -> dict[str, Any]:
    return {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": f"ev_{tool_name}_abcdefghijklmnopqrst",
        "result_hash": f"sha256:{'a' * 64}",
        "domain": domain,
        "data": data,
        "warnings": [],
    }


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self) -> list[str]:
        return sorted(TOOLS)

    async def call(self, tool_name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]:
        self.calls.append((tool_name, {"case_id": case_id, **arguments}))
        payloads = {
            "get_order": evidence(
                tool_name,
                "order",
                {
                    "order_id": ORDER_ID,
                    "order_status": "delivered",
                    "order_purchase_timestamp": "2018-05-11T09:00:00-03:00",
                    "order_delivered_customer_date": "2018-05-20T09:00:00-03:00",
                },
            ),
            "get_order_items": evidence(
                tool_name,
                "item",
                [
                    {
                        "order_item_id": "item-1",
                        "seller_id": "seller-1",
                        "price": "79.00",
                        "freight_value": "18.00",
                    }
                ],
            ),
            "get_product_context": evidence(
                tool_name,
                "product",
                [{"order_item_id": "item-1", "product_id": "product-1"}],
            ),
            "get_sellers": evidence(tool_name, "seller", [{"seller_id": "seller-1"}]),
            "get_shipment_summary": evidence(
                tool_name,
                "shipment",
                {
                    "delivered_customer_at": "2018-05-20T09:00:00-03:00",
                    "estimated_delivery_at": "2018-05-21T09:00:00-03:00",
                    "events": [
                        {
                            "event_at": "2018-01-04T09:00:00-03:00",
                            "event_type": "delivered_late",
                            "actor": "logistics_provider",
                            "status": "confirmed",
                        }
                    ],
                },
            ),
            "get_order_payments": evidence(
                tool_name,
                "payment",
                [{"payment_sequential": "1", "payment_value": "16.00"}],
            ),
            "get_payment_timeline": evidence(
                tool_name,
                "payment",
                {
                    "events": [
                        {
                            "event_at": "2017-12-20T10:00:00-03:00",
                            "event_type": "captured",
                            "amount_brl": "16.00",
                            "status": "confirmed",
                        }
                    ]
                },
            ),
            "get_customer_history": evidence(
                tool_name,
                "customer",
                {
                    "customer_unique_id": "customer-1",
                    "orders": [
                        {
                            "order_id": ORDER_ID,
                            "order_status": "delivered",
                            "order_purchase_timestamp": "2017-12-20T09:00:00-03:00",
                            "order_delivered_customer_date": "2018-01-04T09:00:00-03:00",
                        }
                    ],
                },
            ),
            "get_policy": evidence(
                tool_name,
                "policy",
                {
                    "rules": {
                        "late_delivery_logistics": {
                            "case_status": "action_required",
                            "recommended_action": "refund_freight",
                            "refund_brl": 16.0,
                            "responsible_parties": [
                                {"party_type": "logistics_provider", "party_id": None}
                            ],
                        }
                    }
                },
            ),
        }
        return payloads[tool_name]


class TraceSpy:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, **event: Any) -> dict[str, Any]:
        self.events.append(event)
        return event


def sample_case() -> dict[str, Any]:
    return {
        "case_id": "L3B_CASE_001",
        "opened_at": "2018-01-01T09:00:00-03:00",
        "customer_request": {
            "claimed_order_id": ORDER_ID,
            "claims": [
                {"claim_id": "claim-1", "topic": "late_delivery_logistics"},
                {"claim_id": "claim-2", "topic": "requested_full_refund"},
            ],
        },
        "policy_version": "EC_POLICY_V2",
        "candidate_order_ids": [ORDER_ID, "candidate-001"],
        "investigation_scope": {
            "include_customer_history": True,
            "include_product_context": True,
            "require_independent_verification": True,
        },
        "customer_unique_id_hint": "customer-1",
    }


def test_workflow_is_schema_valid_evidence_scoped_and_call_efficient() -> None:
    gateway = FakeGateway()
    trace = TraceSpy()

    output = asyncio.run(
        solve_case(
            sample_case(),
            gateway,
            trace,  # type: ignore[arg-type]
        )
    )

    root = Path(__file__).resolve().parents[1]
    Contracts(root / "contracts" / "schemas").validate_output(output, "test output")
    called_tools = [name for name, _ in gateway.calls]
    assert called_tools.count("get_order") == 1
    assert "get_refund_timeline" not in called_tools
    assert len(called_tools) == len(set(called_tools)) == 9
    assert output["entity_resolution"] == {
        "status": "resolved",
        "resolved_order_ids": [ORDER_ID],
        "rejected_candidates": ["candidate-001"],
        "confidence": 0.99,
    }
    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"
    assert output["shipment_analysis"]["verdict"] == "logistics_delay"
    assert output["payment_analysis"]["captured_total_brl"] == 16.0
    assert output["financial_resolution"]["recommended_refund_brl"] == 16.0
    assert {event["event_type"] for event in trace.events} >= {
        "task_assigned",
        "tool_result_consumed",
        "handoff",
        "policy_decided",
        "verification_completed",
    }


class ContractSpy:
    def validate_evidence(self, value: Any, label: str) -> None:
        assert value["schema_version"] == "day09-mcp-evidence-v1"
        assert label.startswith("MCP tool")


def test_gateway_supports_mcp_v2_snake_case_result() -> None:
    expected = evidence("get_order", "order", {"order_id": ORDER_ID})
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
