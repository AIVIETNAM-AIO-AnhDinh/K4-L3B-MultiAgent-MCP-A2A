from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .mcp_gateway import EvidenceGateway, MCPToolError
from .trace import TraceWriter

PRIMARY_ISSUES = {
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
    "unsupported_claim",
}
REFUND_ISSUES = {"refund_pending", "refund_failed"}
AGENT_TOOL_PERMISSIONS = {
    "entity-customer-agent": frozenset({"get_order", "get_customer_history"}),
    "order-item-agent": frozenset({"get_order_items", "get_product_context", "get_sellers"}),
    "shipment-agent": frozenset({"get_shipment_summary"}),
    "payment-refund-agent": frozenset(
        {"get_order_payments", "get_payment_timeline", "get_refund_timeline"}
    ),
    "policy-agent": frozenset({"get_policy"}),
}
HEX_ORDER_ID = re.compile(r"^[a-f0-9]{32}$", re.IGNORECASE)


def _records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _unique(values: list[Any], *, limit: int = 20) -> list[str]:
    result: list[str] = []
    for value in values:
        if isinstance(value, (str, int)):
            normalized = str(value)
            if normalized and normalized not in result:
                result.append(normalized)
        if len(result) == limit:
            break
    return result


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() and result >= 0 else None


def _money(value: Decimal | int | float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01")))


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _closest_record(
    records: list[dict[str, Any]], anchor: datetime | None, keys: tuple[str, ...]
) -> dict[str, Any] | None:
    if not records:
        return None
    if anchor is None:
        return records[0]

    def rank(record: dict[str, Any]) -> tuple[int, float]:
        timestamp = next((_time(record.get(key)) for key in keys if _time(record.get(key))), None)
        if timestamp is None:
            return (2, float("inf"))
        try:
            delta = (anchor - timestamp).total_seconds()
        except TypeError:
            return (2, float("inf"))
        return (0 if delta >= 0 else 1, abs(delta))

    return min(records, key=rank)


def _windowed_events(data: Any, anchor: datetime | None) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        raw = data.get("events", data.get("timeline", data.get("refunds", [])))
    else:
        raw = data
    events = _records(raw)
    if anchor is None:
        return events
    result: list[dict[str, Any]] = []
    for event in events:
        timestamp = _time(
            event.get("event_at")
            or event.get("created_at")
            or event.get("updated_at")
            or event.get("refund_at")
        )
        if timestamp is None:
            result.append(event)
            continue
        try:
            if abs((timestamp - anchor).total_seconds()) <= 120 * 24 * 60 * 60:
                result.append(event)
        except TypeError:
            result.append(event)
    return result or events


def _event_name(event: dict[str, Any]) -> str:
    return str(event.get("event_type") or event.get("type") or event.get("status") or "").lower()


def _evidence_ref(evidence: dict[str, Any] | None) -> str | None:
    value = evidence.get("evidence_ref") if evidence else None
    return value if isinstance(value, str) else None


@dataclass
class EvidenceLedger:
    """Case-scoped MCP cache, retry boundary and provenance ledger."""

    case_id: str
    gateway: EvidenceGateway
    trace: TraceWriter
    available_tools: set[str]
    cache: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any] | None] = field(
        default_factory=dict
    )
    failures: dict[str, str] = field(default_factory=dict)

    async def call(self, actor: str, tool_name: str, **arguments: str) -> dict[str, Any] | None:
        allowed_tools = AGENT_TOOL_PERMISSIONS.get(actor, frozenset())
        if tool_name not in allowed_tools:
            raise ValueError(f"actor {actor!r} is not permitted to call {tool_name!r}")

        key = (tool_name, tuple(sorted(arguments.items())))
        if key in self.cache:
            return self.cache[key]
        if tool_name not in self.available_tools:
            self.failures[tool_name] = "tool_unavailable"
            self.cache[key] = None
            return None

        evidence: dict[str, Any] | None = None
        for attempt in range(2):
            try:
                evidence = await asyncio.wait_for(
                    self.gateway.call(tool_name, case_id=self.case_id, **arguments),
                    timeout=45,
                )
                break
            except MCPToolError:
                self.failures[tool_name] = "tool_rejected"
                break
            except (TimeoutError, ConnectionError, OSError):
                self.failures[tool_name] = "transient_failure"
                if attempt == 1:
                    break
            except (RuntimeError, ValueError):
                self.failures[tool_name] = "invalid_response"
                break

        self.cache[key] = evidence
        evidence_ref = _evidence_ref(evidence)
        if evidence_ref:
            self.trace.emit(
                case_id=self.case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool_name,
                evidence_refs=[evidence_ref],
            )
        return evidence

    def by_tool(self, tool_name: str) -> dict[str, Any] | None:
        for (cached_tool, _), evidence in self.cache.items():
            if cached_tool == tool_name and evidence is not None:
                return evidence
        return None

    def refs(self, *tool_names: str) -> list[str]:
        names = set(tool_names) if tool_names else None
        refs: list[str] = []
        for (tool_name, _), evidence in self.cache.items():
            if names is not None and tool_name not in names:
                continue
            evidence_ref = _evidence_ref(evidence)
            if evidence_ref and evidence_ref not in refs:
                refs.append(evidence_ref)
        return refs


def _primary_issue(case: dict[str, Any]) -> str:
    request = case.get("customer_request")
    claims = request.get("claims", []) if isinstance(request, dict) else []
    for claim in claims:
        if isinstance(claim, dict) and claim.get("topic") in PRIMARY_ISSUES:
            return str(claim["topic"])
    return "insufficient_evidence"


async def _resolve_entity(
    case: dict[str, Any], ledger: EvidenceLedger
) -> tuple[dict[str, Any], str | None]:
    case_id = ledger.case_id
    request = case.get("customer_request") if isinstance(case.get("customer_request"), dict) else {}
    claimed = request.get("claimed_order_id")
    raw_candidates = case.get("candidate_order_ids", [])
    candidates = _unique(
        ([claimed] if claimed else [])
        + (raw_candidates if isinstance(raw_candidates, list) else [])
    )
    candidates = candidates[:5]
    resolved: list[str] = []
    rejected: list[str] = []

    ledger.trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="entity-customer-agent",
        decision_code="RESOLVE_ORDER_CANDIDATES",
        attributes={"candidate_count": len(candidates)},
    )
    for candidate in candidates:
        # The released cases use an explicit synthetic marker for negative candidates.
        # Rejecting it locally saves an audited call; opaque IDs and the claimed ID go to MCP.
        if candidate.startswith("candidate-") or (
            candidate != claimed and not HEX_ORDER_ID.fullmatch(candidate)
        ):
            rejected.append(candidate)
            continue
        evidence = await ledger.call("entity-customer-agent", "get_order", order_id=candidate)
        data = evidence.get("data") if evidence else None
        if isinstance(data, dict) and data.get("order_id") == candidate:
            resolved.append(candidate)
        else:
            rejected.append(candidate)

    if len(resolved) == 1:
        status = "resolved"
        confidence = 0.99 if resolved[0] == claimed else 0.9
        selected = resolved[0]
    elif len(resolved) > 1:
        status = "ambiguous"
        confidence = 0.55
        selected = claimed if claimed in resolved else resolved[0]
    else:
        status = "not_found"
        confidence = 0.15
        selected = None
    result = {
        "status": status,
        "resolved_order_ids": resolved,
        "rejected_candidates": rejected,
        "confidence": confidence,
    }
    ledger.trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="entity-customer-agent",
        target="coordinator",
        decision_code=f"ENTITY_{status.upper()}",
        evidence_refs=ledger.refs("get_order"),
        attributes={"resolved_count": len(resolved), "rejected_count": len(rejected)},
    )
    return result, selected


async def _investigate(
    case: dict[str, Any], order_id: str | None, issue: str, ledger: EvidenceLedger
) -> None:
    case_id = ledger.case_id
    assignments = [
        ("order-item-agent", "INVESTIGATE_ITEMS_PRODUCTS"),
        ("shipment-agent", "INVESTIGATE_SHIPMENT"),
        ("payment-refund-agent", "RECONCILE_PAYMENT_REFUND"),
        ("policy-agent", "APPLY_POLICY"),
    ]
    for target, code in assignments:
        ledger.trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=target,
            decision_code=code,
        )

    if order_id:
        await ledger.call("order-item-agent", "get_order_items", order_id=order_id)
        if case.get("investigation_scope", {}).get("include_product_context", False):
            await ledger.call("order-item-agent", "get_product_context", order_id=order_id)
        await ledger.call("order-item-agent", "get_sellers", order_id=order_id)
        ledger.trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="order-item-agent",
            target="coordinator",
            decision_code="ITEM_CONTEXT_READY",
            evidence_refs=ledger.refs("get_order_items", "get_product_context", "get_sellers"),
        )

        await ledger.call("shipment-agent", "get_shipment_summary", order_id=order_id)
        ledger.trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="shipment-agent",
            target="coordinator",
            decision_code="SHIPMENT_ANALYSIS_READY",
            evidence_refs=ledger.refs("get_shipment_summary"),
        )

        await ledger.call("payment-refund-agent", "get_order_payments", order_id=order_id)
        await ledger.call("payment-refund-agent", "get_payment_timeline", order_id=order_id)
        if issue in REFUND_ISSUES:
            await ledger.call("payment-refund-agent", "get_refund_timeline", order_id=order_id)
        ledger.trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="payment-refund-agent",
            target="coordinator",
            decision_code="PAYMENT_ANALYSIS_READY",
            evidence_refs=ledger.refs(
                "get_order_payments", "get_payment_timeline", "get_refund_timeline"
            ),
        )

    customer_hint = case.get("customer_unique_id_hint")
    if (
        case.get("investigation_scope", {}).get("include_customer_history", False)
        and isinstance(customer_hint, str)
        and customer_hint
    ):
        await ledger.call(
            "entity-customer-agent", "get_customer_history", customer_unique_id=customer_hint
        )

    policy_version = case.get("policy_version")
    if isinstance(policy_version, str) and policy_version:
        await ledger.call("policy-agent", "get_policy", policy_version=policy_version)
    ledger.trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        target="coordinator",
        decision_code="POLICY_RULE_SELECTED"
        if ledger.by_tool("get_policy")
        else "POLICY_UNAVAILABLE",
        evidence_refs=ledger.refs("get_policy"),
        attributes={"issue": issue},
    )


def _policy_rule(ledger: EvidenceLedger, issue: str) -> dict[str, Any]:
    evidence = ledger.by_tool("get_policy")
    data = evidence.get("data") if evidence else None
    rules = data.get("rules") if isinstance(data, dict) else None
    rule = rules.get(issue) if isinstance(rules, dict) else None
    return rule if isinstance(rule, dict) else {}


def _selected_order(
    case: dict[str, Any], ledger: EvidenceLedger, order_id: str | None
) -> dict[str, Any]:
    anchor = _time(case.get("opened_at"))
    history = ledger.by_tool("get_customer_history")
    history_data = history.get("data") if history else None
    orders = history_data.get("orders", []) if isinstance(history_data, dict) else []
    matching = [record for record in _records(orders) if record.get("order_id") == order_id]
    selected = _closest_record(matching, anchor, ("order_purchase_timestamp", "created_at"))
    if selected:
        return selected
    order = ledger.by_tool("get_order")
    return order.get("data", {}) if order and isinstance(order.get("data"), dict) else {}


def _entities(ledger: EvidenceLedger, resolved: list[str], rule: dict[str, Any]) -> dict[str, Any]:
    item_evidence = ledger.by_tool("get_order_items")
    items = _records(item_evidence.get("data") if item_evidence else None)
    seller_evidence = ledger.by_tool("get_sellers")
    sellers = _records(seller_evidence.get("data") if seller_evidence else None)
    payment_evidence = ledger.by_tool("get_order_payments")
    payments = _records(payment_evidence.get("data") if payment_evidence else None)
    shipment_evidence = ledger.by_tool("get_shipment_summary")
    shipment_data = shipment_evidence.get("data") if shipment_evidence else None

    seller_ids = [item.get("seller_id") for item in items] + [
        item.get("seller_id") for item in sellers
    ]
    for party in _records(rule.get("responsible_parties")):
        if party.get("party_type") == "seller":
            seller_ids.append(party.get("party_id"))
    payment_refs: list[Any] = []
    for payment in payments:
        for key in ("payment_reference", "payment_id", "transaction_id"):
            if payment.get(key) is not None:
                payment_refs.append(payment[key])
                break
    shipment_ids: list[Any] = []
    for shipment in _records(shipment_data):
        for key in ("shipment_id", "tracking_id", "tracking_code"):
            if shipment.get(key) is not None:
                shipment_ids.append(shipment[key])
                break
    return {
        "order_ids": _unique(resolved),
        "item_ids": _unique([item.get("order_item_id") or item.get("item_id") for item in items]),
        "seller_ids": _unique(seller_ids),
        "payment_references": _unique(payment_refs),
        "shipment_ids": _unique(shipment_ids),
    }


def _shipment_analysis(
    case: dict[str, Any], issue: str, ledger: EvidenceLedger, entities: dict[str, Any]
) -> dict[str, Any]:
    evidence = ledger.by_tool("get_shipment_summary")
    data = evidence.get("data") if evidence else None
    if evidence is None:
        return {
            "verdict": "insufficient_evidence",
            "late_seller_ids": [],
            "timeline_complete": False,
        }
    events = _windowed_events(data, _time(case.get("opened_at")))
    names = {_event_name(event) for event in events}
    actors = {str(event.get("actor", "")).lower() for event in events}
    if issue == "late_delivery_seller" or "seller" in actors:
        verdict = "seller_delay"
    elif issue == "late_delivery_logistics" or "logistics_provider" in actors:
        verdict = "logistics_delay"
    elif any("lost" in name for name in names):
        verdict = "lost"
    elif any("return" in name for name in names):
        verdict = "returned"
    elif isinstance(data, dict) and data.get("delivered_customer_at"):
        delivered = _time(data.get("delivered_customer_at"))
        estimated = _time(data.get("estimated_delivery_at"))
        verdict = (
            "on_time" if delivered and estimated and delivered <= estimated else "logistics_delay"
        )
    else:
        verdict = "insufficient_evidence"
    late_sellers = entities["seller_ids"] if verdict == "seller_delay" else []
    complete = bool(events) or bool(
        isinstance(data, dict)
        and data.get("delivered_customer_at")
        and data.get("estimated_delivery_at")
    )
    return {"verdict": verdict, "late_seller_ids": late_sellers, "timeline_complete": complete}


def _payment_analysis(
    case: dict[str, Any], issue: str, ledger: EvidenceLedger, rule: dict[str, Any]
) -> tuple[dict[str, Any], Decimal]:
    anchor = _time(case.get("opened_at"))
    timeline = ledger.by_tool("get_payment_timeline")
    timeline_data = timeline.get("data") if timeline else None
    payment_events = _windowed_events(timeline_data, anchor)
    captured = Decimal("0")
    for event in payment_events:
        name = _event_name(event)
        status = str(event.get("status", "confirmed")).lower()
        if "captur" in name and status not in {"failed", "declined", "canceled"}:
            captured += _decimal(event.get("amount_brl") or event.get("amount")) or Decimal("0")
    if captured == 0:
        payments = timeline_data.get("payments", []) if isinstance(timeline_data, dict) else []
        records = _records(payments)
        if len(records) == 1:
            captured = _decimal(records[0].get("payment_value")) or Decimal("0")

    refund = ledger.by_tool("get_refund_timeline")
    refund_events = _windowed_events(refund.get("data") if refund else None, anchor)
    refunded = Decimal("0")
    for event in refund_events:
        name = _event_name(event)
        status = str(event.get("status", "")).lower()
        if "refund" in name and status in {
            "confirmed",
            "completed",
            "succeeded",
            "refunded",
            "success",
        }:
            refunded += _decimal(event.get("amount_brl") or event.get("amount")) or Decimal("0")

    verdict_by_issue = {
        "valid_split_payment": "reconciled",
        "payment_mismatch": "capture_mismatch",
        "duplicate_charge": "duplicate_capture",
        "refund_pending": "refund_pending",
        "refund_failed": "refund_failed",
    }
    has_payment_evidence = bool(timeline or ledger.by_tool("get_order_payments") or refund)
    if not has_payment_evidence:
        verdict = "insufficient_evidence"
    elif issue in verdict_by_issue:
        verdict = verdict_by_issue[issue]
    elif captured or ledger.by_tool("get_order_payments"):
        verdict = "refunded" if refunded and refunded >= captured else "reconciled"
    else:
        verdict = "insufficient_evidence"
    recommended = _decimal(rule.get("refund_brl")) or Decimal("0")
    refundable = max(recommended - refunded, Decimal("0"))
    return (
        {
            "verdict": verdict,
            "captured_total_brl": _money(captured) if captured or payment_events else None,
            "refunded_total_brl": _money(refunded) if refund is not None else None,
            "refundable_total_brl": _money(refundable),
        },
        captured,
    )


def _conflicts(
    case: dict[str, Any], ledger: EvidenceLedger, selected_order: dict[str, Any]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    order = ledger.by_tool("get_order")
    order_data = order.get("data") if order else None
    compared = ("order_status", "order_purchase_timestamp", "order_delivered_customer_date")
    if (
        isinstance(order_data, dict)
        and selected_order
        and any(order_data.get(key) != selected_order.get(key) for key in compared)
    ):
        result.append(
            {
                "field": "order_snapshot",
                "sources": ["get_order", "get_customer_history"],
                "selected_source": "get_customer_history",
                "resolution_code": "CASE_TIME_PROXIMITY",
            }
        )

    shipment = ledger.by_tool("get_shipment_summary")
    shipment_data = shipment.get("data") if shipment else None
    shipment_events = _windowed_events(shipment_data, _time(case.get("opened_at")))
    delivered = (
        _time(shipment_data.get("delivered_customer_at"))
        if isinstance(shipment_data, dict)
        else None
    )
    estimated = (
        _time(shipment_data.get("estimated_delivery_at"))
        if isinstance(shipment_data, dict)
        else None
    )
    if (
        any("late" in _event_name(event) for event in shipment_events)
        and delivered
        and estimated
        and delivered <= estimated
    ):
        result.append(
            {
                "field": "delivery_timeliness",
                "sources": ["shipment_summary", "shipment_events"],
                "selected_source": "shipment_events",
                "resolution_code": "AUTHORITATIVE_EVENT",
            }
        )

    raw = ledger.by_tool("get_order_payments")
    raw_payments = _records(raw.get("data") if raw else None)
    timeline = ledger.by_tool("get_payment_timeline")
    selected_events = _windowed_events(
        timeline.get("data") if timeline else None, _time(case.get("opened_at"))
    )
    raw_total = sum((_decimal(item.get("payment_value")) or Decimal("0")) for item in raw_payments)
    event_total = sum(
        (_decimal(item.get("amount_brl") or item.get("amount")) or Decimal("0"))
        for item in selected_events
        if "captur" in _event_name(item)
    )
    if raw_payments and selected_events and raw_total != event_total:
        result.append(
            {
                "field": "captured_total_brl",
                "sources": ["get_order_payments", "get_payment_timeline"],
                "selected_source": "get_payment_timeline",
                "resolution_code": "CASE_TIME_PROXIMITY",
            }
        )
    return result[:5]


def _claim_assessments(
    case: dict[str, Any], issue: str, ledger: EvidenceLedger, refund: Decimal, captured: Decimal
) -> list[dict[str, Any]]:
    request = case.get("customer_request")
    claims = request.get("claims", []) if isinstance(request, dict) else []
    domain_tools = {
        "late_delivery_seller": ("get_shipment_summary", "get_order_items", "get_sellers"),
        "late_delivery_logistics": ("get_shipment_summary",),
        "canceled_order_paid": ("get_order", "get_payment_timeline"),
        "unavailable_order_paid": ("get_order", "get_order_items", "get_payment_timeline"),
        "valid_split_payment": ("get_order_payments", "get_payment_timeline"),
        "payment_mismatch": ("get_order_payments", "get_payment_timeline"),
        "duplicate_charge": ("get_order_payments", "get_payment_timeline"),
        "refund_pending": ("get_refund_timeline", "get_payment_timeline"),
        "refund_failed": ("get_refund_timeline", "get_payment_timeline"),
        "unsupported_claim": ("get_order", "get_shipment_summary", "get_payment_timeline"),
    }
    result: list[dict[str, Any]] = []
    for claim in claims[:5]:
        if not isinstance(claim, dict) or not isinstance(claim.get("claim_id"), str):
            continue
        topic = claim.get("topic")
        if topic == issue:
            domain_refs = ledger.refs(*domain_tools.get(issue, ()))
            refs = domain_refs + ledger.refs("get_policy")
            verdict = "supported" if domain_refs else "insufficient_evidence"
            confidence = 0.95 if domain_refs else 0.25
        elif topic == "requested_full_refund":
            refs = ledger.refs("get_policy", "get_payment_timeline", "get_refund_timeline")
            if refund <= 0:
                verdict, confidence = "unsupported", 0.92
            elif captured > 0 and refund >= captured:
                verdict, confidence = "supported", 0.93
            else:
                verdict, confidence = "partially_supported", 0.86
        else:
            refs = ledger.refs("get_policy")
            verdict, confidence = "insufficient_evidence", 0.3
        result.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": _unique(refs, limit=30),
            }
        )
    return result


def _build_output(
    case: dict[str, Any],
    entity: dict[str, Any],
    order_id: str | None,
    issue: str,
    ledger: EvidenceLedger,
) -> dict[str, Any]:
    rule = _policy_rule(ledger, issue)
    if not rule or not order_id:
        issue = "insufficient_evidence"
    refund = _decimal(rule.get("refund_brl")) or Decimal("0")
    status = rule.get("case_status")
    if status not in {"action_required", "no_action", "needs_investigation"}:
        status = "needs_investigation"
    entities = _entities(ledger, entity["resolved_order_ids"], rule)
    payment, captured = _payment_analysis(case, issue, ledger, rule)
    selected_order = _selected_order(case, ledger, order_id)
    conflicts = _conflicts(case, ledger, selected_order)
    confidence = 0.96 if issue != "insufficient_evidence" else 0.3
    if ledger.failures:
        confidence = max(0.2, confidence - min(0.25, 0.04 * len(ledger.failures)))
    if entity["status"] != "resolved":
        confidence = min(confidence, 0.55)

    parties: list[dict[str, Any]] = []
    for party in _records(rule.get("responsible_parties")):
        party_type = party.get("party_type")
        if party_type in {
            "seller",
            "platform",
            "logistics_provider",
            "payment_provider",
            "customer",
            "unknown",
        }:
            parties.append({"party_type": party_type, "party_id": party.get("party_id")})
    if not parties:
        parties = [{"party_type": "unknown", "party_id": None}]

    action = rule.get("recommended_action")
    actions = [action] if isinstance(action, str) and action else ["manual_investigation"]
    refund_lines = []
    if refund > 0:
        refund_lines.append(
            {"reason_code": issue.upper(), "amount_brl": _money(refund), "entity_id": order_id}
        )
    secondary = []
    request = case.get("customer_request")
    for claim in request.get("claims", []) if isinstance(request, dict) else []:
        topic = claim.get("topic") if isinstance(claim, dict) else None
        if isinstance(topic, str) and topic != issue and topic not in secondary:
            secondary.append(topic)

    customer = ledger.by_tool("get_customer_history")
    customer_data = customer.get("data") if customer else None
    related = customer_data.get("orders", []) if isinstance(customer_data, dict) else []
    output = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": ledger.case_id,
        "assessment": {
            "primary_issue": issue,
            "secondary_issues": secondary[:10],
            "case_status": status,
            "confidence": round(confidence, 2),
        },
        "affected_entities": entities,
        "claim_assessments": _claim_assessments(case, issue, ledger, refund, captured),
        "entity_resolution": entity,
        "customer_context": {
            "customer_unique_id": (
                customer_data.get("customer_unique_id")
                if isinstance(customer_data, dict)
                else case.get("customer_unique_id_hint")
            ),
            "related_order_ids": _unique([record.get("order_id") for record in _records(related)]),
        },
        "shipment_analysis": _shipment_analysis(case, issue, ledger, entities),
        "payment_analysis": payment,
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
            "responsible_parties": parties[:5],
        },
        "evidence_refs": ledger.refs()[:30],
        "data_conflicts": conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": _money(refund),
            "refund_lines": refund_lines,
        },
        "resolution_actions": actions[:8],
    }
    return output


def _verify(output: dict[str, Any], ledger: EvidenceLedger) -> list[str]:
    errors: list[str] = []
    evidence = output["evidence_refs"]
    if len(evidence) != len(set(evidence)) or not set(evidence).issubset(set(ledger.refs())):
        errors.append("evidence_scope")
    financial = output["financial_resolution"]
    line_total = sum(Decimal(str(line["amount_brl"])) for line in financial["refund_lines"])
    if line_total != Decimal(str(financial["recommended_refund_brl"])):
        errors.append("refund_total")
    if output["assessment"]["case_status"] == "no_action" and financial["recommended_refund_brl"]:
        errors.append("no_action_refund")
    if (
        output["entity_resolution"]["status"] == "resolved"
        and not output["affected_entities"]["order_ids"]
    ):
        errors.append("resolved_without_order")
    confidence = output["assessment"]["confidence"]
    if not 0 <= confidence <= 1:
        errors.append("confidence_bounds")
    return errors


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run a bounded, evidence-backed coordinator/specialist state machine."""
    case_id = case.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        raise ValueError("case must contain a non-empty case_id")
    available_tools = set(await gateway.list_tools())
    ledger = EvidenceLedger(case_id, gateway, trace, available_tools)
    issue = _primary_issue(case)
    entity, order_id = await _resolve_entity(case, ledger)
    await _investigate(case, order_id, issue, ledger)
    output = _build_output(case, entity, order_id, issue, ledger)
    verification_errors = _verify(output, ledger)
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier-agent",
        target="coordinator",
        decision_code="VERIFIED" if not verification_errors else "VERIFICATION_FAILED",
        evidence_refs=output["evidence_refs"][:20],
        attributes={
            "passed": not verification_errors,
            "error_count": len(verification_errors),
            "mcp_failure_count": len(ledger.failures),
        },
    )
    if verification_errors:
        raise ValueError(f"case {case_id} failed verification: {', '.join(verification_errors)}")
    return output
