"""Coordinator: plans the bounded DAG, dispatches A2A tasks, merges facts, builds output.

It is the only component that writes the output, and it never calls a domain tool itself.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any

from .a2a import MAX_HOPS, A2AResult, A2ATask
from .base import SpecialistAgent
from .entity import EntityCustomerAgent
from .ledger import EvidenceLedger
from .order_item import OrderItemAgent
from .parsing import money, pick, unique
from .payment import PaymentRefundAgent
from .policy import PolicyAgent
from .shipment import ShipmentAgent
from .verifier import assess_issue, infer_issue_from_facts, verify

ACTOR = "coordinator"
PRIMARY_ISSUES = {
    "canceled_order_paid", "unavailable_order_paid", "late_delivery_seller",
    "late_delivery_logistics", "valid_split_payment", "payment_mismatch", "duplicate_charge",
    "refund_pending", "refund_failed", "unsupported_claim",
}
SHIPMENT_ISSUES = {"late_delivery_seller", "late_delivery_logistics"}
REFUND_ISSUES = {"refund_pending", "refund_failed"}
SNAPSHOT_ISSUES = {"valid_split_payment", "payment_mismatch", "duplicate_charge"}
ITEM_ISSUES = SHIPMENT_ISSUES | {"unavailable_order_paid", "valid_split_payment",
                                 "payment_mismatch"}


def issue_mode() -> str:
    """`claim` preserves the better-scoring baseline; `evidence` is an A/B override."""
    mode = os.getenv("DAY09_ISSUE_MODE", "claim").strip().lower()
    return mode if mode in {"claim", "evidence"} else "claim"


def claimed_issue(case: dict[str, Any]) -> str:
    request = case.get("customer_request")
    claims = request.get("claims", []) if isinstance(request, dict) else []
    for claim in claims:
        if isinstance(claim, dict) and claim.get("topic") in PRIMARY_ISSUES:
            return str(claim["topic"])
    return "insufficient_evidence"


class Coordinator:
    def __init__(self, case: dict[str, Any], ledger: EvidenceLedger) -> None:
        self.case = case
        self.ledger = ledger
        self.case_id = ledger.case_id
        self.hop = 0
        self.conflicts: list[dict[str, Any]] = []
        self.entity = EntityCustomerAgent()
        self.items = OrderItemAgent()
        self.shipment = ShipmentAgent()
        self.payment = PaymentRefundAgent()
        self.policy = PolicyAgent()

    # ---- A2A dispatch ---------------------------------------------------------------
    async def dispatch(
        self, agent: SpecialistAgent, task: str, payload: dict[str, Any]
    ) -> A2AResult:
        self.hop += 1
        if self.hop > MAX_HOPS:
            raise RuntimeError(f"{self.case_id}: A2A hop limit exceeded")
        message = A2ATask(self.case_id, ACTOR, agent.name, task, self.hop, payload)
        self.ledger.trace.emit(
            case_id=self.case_id, event_type="task_assigned", actor=ACTOR, target=agent.name,
            decision_code=task, attributes={"hop": self.hop},
        )
        result = await agent.handle(message, self.ledger)
        if result.case_id != self.case_id:  # correlation guard: never merge cross-case facts
            raise RuntimeError(f"{self.case_id}: specialist returned {result.case_id}")
        self.conflicts.extend(result.conflicts)
        self.ledger.trace.emit(
            case_id=self.case_id,
            event_type="policy_decided" if agent is self.policy else "handoff",
            actor=agent.name, target=ACTOR, decision_code=result.decision_code,
            evidence_refs=result.evidence_refs[:20],
            attributes={"hop": self.hop, "conflicts": len(result.conflicts)},
        )
        return result

    # ---- workflow -------------------------------------------------------------------
    async def run(self) -> dict[str, Any]:
        case = self.case
        request = case.get("customer_request") if isinstance(case.get("customer_request"),
                                                               dict) else {}
        scope = case.get("investigation_scope") if isinstance(case.get("investigation_scope"),
                                                              dict) else {}
        opened_at = case.get("opened_at")
        hypothesis = claimed_issue(case)

        entity = await self.dispatch(self.entity, "RESOLVE_ENTITY", {
            "claimed_order_id": request.get("claimed_order_id"),
            "candidates": case.get("candidate_order_ids") or [],
            "customer_unique_id_hint": case.get("customer_unique_id_hint"),
            "opened_at": opened_at,
            "include_customer_history": scope.get("include_customer_history", True),
        })
        order_id = entity.facts["selected_order_id"]
        facts: dict[str, Any] = {"entity": entity.facts, "order": entity.facts.get("order", {}),
                                 "items": {}, "shipment": {}, "payment": {}}

        purchased_at = pick(facts["order"], "order_purchase_timestamp", "purchase_timestamp",
                            "purchased_at", "created_at")
        if order_id:
            if hypothesis in ITEM_ISSUES:
                items = await self.dispatch(self.items, "INVESTIGATE_ITEMS_PRODUCTS", {
                    "order_id": order_id,
                    "need_product_context": hypothesis == "unavailable_order_paid"
                    and scope.get("include_product_context", True),
                })
                facts["items"] = items.facts
            shipment = await self.dispatch(self.shipment, "INVESTIGATE_SHIPMENT", {
                "order_id": order_id, "opened_at": opened_at, "order": facts["order"],
                "purchased_at": purchased_at,
                "shipping_limits": facts["items"].get("shipping_limits", []),
            })
            facts["shipment"] = shipment.facts
            payment = await self.dispatch(self.payment, "RECONCILE_PAYMENT_REFUND", {
                "order_id": order_id, "opened_at": opened_at, "purchased_at": purchased_at,
                "need_payment_snapshot": hypothesis in SNAPSHOT_ISSUES,
                "need_refund_timeline": hypothesis in REFUND_ISSUES,
                "expected_total_brl": facts["items"].get("order_total_brl")
                or pick(facts["order"], "order_total_brl", "total_brl", "order_value"),
            })
            facts["payment"] = payment.facts

        # independent verification of the claim before the policy is applied
        support, alternative = assess_issue(hypothesis, facts) if order_id else ("unknown", None)
        issue = hypothesis if order_id else "insufficient_evidence"
        issue_support = support
        if order_id and issue_mode() == "evidence":
            inferred = infer_issue_from_facts(facts)
            if support == "contradicted":
                issue = (
                    alternative if alternative not in (None, "unsupported_claim") else inferred
                ) or alternative or "unsupported_claim"
            elif hypothesis == "unsupported_claim" and inferred:
                issue = inferred
            if issue != hypothesis:
                issue_support, _ = assess_issue(issue, facts)

        policy_facts = {**facts["payment"], **{
            k: facts["items"].get(k) for k in ("freight_total_brl", "items_total_brl")
        }}
        policy = await self.dispatch(self.policy, "APPLY_POLICY", {
            "issue": issue, "policy_version": case.get("policy_version"), "facts": policy_facts,
        })
        if not policy.facts["found"] and issue != "insufficient_evidence":
            issue = "insufficient_evidence"
            issue_support = "unknown"
            policy.facts.update(case_status="needs_investigation",
                                actions=["manual_investigation"], refund_amount=Decimal(0))

        output = self.build(case, facts, policy.facts, issue, hypothesis, support, issue_support)

        self.ledger.trace.emit(
            case_id=self.case_id, event_type="task_assigned", actor=ACTOR,
            target="verifier-agent", decision_code="VERIFY_DRAFT",
        )
        repairs, errors = verify(output, self.ledger, facts)
        self.ledger.trace.emit(
            case_id=self.case_id, event_type="verification_completed", actor="verifier-agent",
            target=ACTOR,
            decision_code="VERIFIED" if not errors else "VERIFICATION_DEGRADED",
            evidence_refs=output["evidence_refs"][:20],
            attributes={
                "passed": not errors, "error_count": len(errors), "repair_count": len(repairs),
                "claim_support": support, "issue_mode": issue_mode(),
                "mcp_calls": self.ledger.audited_calls,
                "mcp_failure_count": len(self.ledger.failures),
            },
        )
        if errors:  # fail closed on the conclusion, but still emit a scorable output
            output["assessment"].update(case_status="needs_investigation",
                                        confidence=min(output["assessment"]["confidence"], 0.3))
        return output

    # ---- output ---------------------------------------------------------------------
    def build(
        self, case: dict[str, Any], facts: dict[str, Any], policy: dict[str, Any], issue: str,
        hypothesis: str, support: str, issue_support: str,
    ) -> dict[str, Any]:
        entity, items = facts["entity"], facts["items"]
        shipment, payment = facts["shipment"], facts["payment"]
        order_id = entity["selected_order_id"]
        refund: Decimal = policy["refund_amount"]
        if issue == "insufficient_evidence":
            refund = Decimal(0)
        refund = refund.quantize(Decimal("0.01")) if refund > 0 else Decimal(0)

        seller_ids = unique(items.get("seller_ids", []))
        parties = [dict(p) for p in policy["responsible_parties"]][:5]

        confidence = {"supported": 0.93, "unknown": 0.82, "contradicted": 0.6}[support]
        if issue != hypothesis and issue != "insufficient_evidence":
            confidence = 0.72
        if issue == "insufficient_evidence":
            confidence = 0.7 if entity["status"] == "not_found" else 0.35
        if entity["status"] == "ambiguous":
            confidence = min(confidence, 0.55)
        confidence -= min(0.2, 0.05 * len(self.ledger.failures))
        confidence = round(max(0.05, min(confidence, 0.99)), 2)

        claims = self._claims(case, issue, issue_support, refund, payment)
        shipment_verdict = shipment.get("verdict", "insufficient_evidence")
        output = {
            "schema_version": "day09-l3b-output-v2",
            "case_id": self.case_id,
            "assessment": {
                "primary_issue": issue,
                "secondary_issues": [],
                "case_status": policy["case_status"],
                "confidence": confidence,
            },
            "affected_entities": {
                "order_ids": [order_id] if order_id else [],
                "item_ids": unique(items.get("item_ids", [])),
                "seller_ids": seller_ids,
                "payment_references": unique(payment.get("payment_references", [])),
                "shipment_ids": unique(shipment.get("shipment_ids", [])),
            },
            "claim_assessments": claims,
            "entity_resolution": {
                "status": entity["status"],
                "resolved_order_ids": unique(entity["resolved_order_ids"]),
                "rejected_candidates": unique(entity["rejected_candidates"]),
                "confidence": entity["confidence"],
            },
            "customer_context": {
                "customer_unique_id": entity.get("customer_unique_id"),
                "related_order_ids": unique(entity.get("related_order_ids", [])),
            },
            "shipment_analysis": {
                "verdict": shipment_verdict,
                "late_seller_ids": seller_ids if shipment_verdict == "seller_delay" else [],
                "timeline_complete": bool(shipment.get("timeline_complete")),
            },
            "payment_analysis": {
                "verdict": payment.get("verdict", "insufficient_evidence"),
                "captured_total_brl": payment.get("captured_total_brl"),
                "refunded_total_brl": money(payment["refunded"])
                if payment.get("timeline_available") or payment.get("refund_source") else None,
                "refundable_total_brl": money(refund),
            },
            "root_cause_analysis": {
                "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
                "responsible_parties": parties,
            },
            "evidence_refs": self.ledger.refs()[:30],
            "data_conflicts": self._dedupe_conflicts(),
            "financial_resolution": {
                "currency": "BRL",
                "recommended_refund_brl": money(refund),
                "refund_lines": [{"reason_code": issue.upper(), "amount_brl": money(refund),
                                  "entity_id": order_id}] if refund > 0 else [],
            },
            "resolution_actions": list(dict.fromkeys(policy["actions"]))[:8],
        }
        return output

    def _claims(
        self, case: dict[str, Any], issue: str, support: str, refund: Decimal,
        payment: dict[str, Any],
    ) -> list[dict[str, Any]]:
        request = case.get("customer_request")
        claims = request.get("claims", []) if isinstance(request, dict) else []
        ledger = self.ledger
        domain_refs = {
            "late_delivery_seller": ("get_shipment_summary", "get_order_items"),
            "late_delivery_logistics": ("get_shipment_summary",),
            "canceled_order_paid": ("get_order", "get_payment_timeline"),
            "unavailable_order_paid": ("get_order", "get_order_items", "get_product_context",
                                       "get_payment_timeline"),
            "valid_split_payment": ("get_order_payments", "get_payment_timeline"),
            "payment_mismatch": ("get_order_payments", "get_payment_timeline",
                                 "get_order_items"),
            "duplicate_charge": ("get_order_payments", "get_payment_timeline"),
            "refund_pending": ("get_refund_timeline", "get_payment_timeline"),
            "refund_failed": ("get_refund_timeline", "get_payment_timeline"),
            "unsupported_claim": ("get_order", "get_shipment_summary", "get_payment_timeline"),
        }
        captured: Decimal = payment.get("captured") or Decimal(0)
        result: list[dict[str, Any]] = []
        for claim in claims[:5]:
            if not isinstance(claim, dict) or not isinstance(claim.get("claim_id"), str):
                continue
            topic = claim.get("topic")
            if topic in PRIMARY_ISSUES:
                refs = ledger.refs(*domain_refs.get(str(topic), ())) + ledger.refs("get_policy")
                if issue == "insufficient_evidence" or not refs:
                    verdict, confidence = "insufficient_evidence", 0.4
                elif topic == "unsupported_claim":
                    verdict, confidence = "unsupported", 0.85
                elif topic != issue or support == "contradicted":
                    verdict, confidence = "unsupported", 0.7
                else:
                    verdict = "supported"
                    confidence = 0.93 if support == "supported" else 0.8
            elif topic == "requested_full_refund":
                refs = ledger.refs("get_payment_timeline", "get_refund_timeline", "get_policy")
                if issue == "insufficient_evidence":
                    verdict, confidence = "insufficient_evidence", 0.4
                elif refund <= 0:
                    verdict, confidence = "unsupported", 0.9
                elif captured > 0 and refund + Decimal("0.01") >= captured:
                    verdict, confidence = "supported", 0.9
                else:
                    verdict, confidence = "partially_supported", 0.85
            else:
                refs = ledger.refs("get_policy")
                verdict, confidence = "insufficient_evidence", 0.3
            result.append({
                "claim_id": claim["claim_id"][:64], "verdict": verdict,
                "confidence": confidence, "evidence_refs": list(dict.fromkeys(refs))[:30],
            })
        return result

    def _dedupe_conflicts(self) -> list[dict[str, Any]]:
        seen: set[str] = set()
        result = []
        for conflict in self.conflicts:
            if conflict["field"] in seen:
                continue
            seen.add(conflict["field"])
            result.append(conflict)
        return result[:5]
