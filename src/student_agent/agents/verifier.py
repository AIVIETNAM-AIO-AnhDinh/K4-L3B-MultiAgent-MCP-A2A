"""Verifier agent: independent claim check + output invariants (no tool calls).

It re-derives support for the claimed issue from specialist facts and checks the draft
against the ledger. Recoverable violations are repaired deterministically and reported;
nothing is fabricated — a repair can only remove refs/money or lower confidence.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .ledger import EvidenceLedger
from .parsing import CENT, money

LATE_ISSUES = {"late_delivery_seller": "seller", "late_delivery_logistics": "logistics_provider"}


def infer_issue_from_facts(facts: dict[str, Any]) -> str | None:
    """Return a known issue directly supported by specialist facts, if one is clear."""
    shipment = facts.get("shipment", {})
    shipment_issue = {
        "seller_delay": "late_delivery_seller",
        "logistics_delay": "late_delivery_logistics",
    }.get(shipment.get("verdict"))
    if shipment_issue:
        return shipment_issue

    payment = facts.get("payment", {})
    payment_issue = {
        "capture_mismatch": "payment_mismatch",
        "duplicate_capture": "duplicate_charge",
        "refund_pending": "refund_pending",
        "refund_failed": "refund_failed",
    }.get(payment.get("verdict"))
    if payment_issue:
        return payment_issue

    order = facts.get("order", {})
    status = str(order.get("order_status") or order.get("status") or "").lower()
    captured: Decimal = payment.get("captured") or Decimal(0)
    if "cancel" in status and captured > 0:
        return "canceled_order_paid"
    product_status = " ".join(facts.get("items", {}).get("product_status", []))
    if "unavailable" in product_status and captured > 0:
        return "unavailable_order_paid"
    if payment.get("verdict") == "reconciled" and payment.get("split"):
        return "valid_split_payment"
    return None


def assess_issue(issue: str, facts: dict[str, Any]) -> tuple[str, str | None]:
    """Return (support, alternative_issue); support in supported|contradicted|unknown."""
    shipment = facts.get("shipment", {})
    payment = facts.get("payment", {})
    order = facts.get("order", {})
    items = facts.get("items", {})
    status = str(order.get("order_status") or order.get("status") or "").lower()
    captured: Decimal = payment.get("captured") or Decimal(0)
    has_captures = bool(payment.get("capture_count"))

    if issue in LATE_ISSUES:
        late, who = shipment.get("late"), shipment.get("attribution")
        if late is None:
            return "unknown", None
        if late is False:
            return "contradicted", "unsupported_claim"
        if who and who != LATE_ISSUES[issue]:
            other = next(k for k, v in LATE_ISSUES.items() if v == who)
            return "contradicted", other
        return ("supported" if who else "unknown"), None
    if issue in {"canceled_order_paid", "unavailable_order_paid"}:
        wanted = "cancel" if issue == "canceled_order_paid" else "unavailable"
        product_flags = " ".join(items.get("product_status", []))
        matches = wanted in status or (wanted == "unavailable" and "unavailable" in product_flags)
        if matches and captured > 0:
            return "supported", None
        if status and not matches:
            return "contradicted", "unsupported_claim"
        if has_captures and captured == 0:
            return "contradicted", "unsupported_claim"
        return "unknown", None
    verdict = payment.get("verdict")
    if issue == "valid_split_payment":
        if verdict == "duplicate_capture":
            return "contradicted", "duplicate_charge"
        if verdict == "capture_mismatch":
            return "contradicted", "payment_mismatch"
        return ("supported" if payment.get("split") else "unknown"), None
    if issue == "payment_mismatch":
        if verdict == "capture_mismatch":
            return "supported", None
        if verdict == "duplicate_capture":
            return "contradicted", "duplicate_charge"
        return "unknown", None
    if issue == "duplicate_charge":
        if verdict == "duplicate_capture":
            return "supported", None
        if has_captures and payment.get("capture_count") == 1:
            return "contradicted", "unsupported_claim"
        return "unknown", None
    if issue in {"refund_pending", "refund_failed"}:
        states = set(payment.get("refund_states", []))
        wanted = "pending" if issue == "refund_pending" else "failed"
        if wanted in states:
            return "supported", None
        other = "failed" if wanted == "pending" else "pending"
        if other in states:
            return "contradicted", f"refund_{other}"
        if "completed" in states:
            return "contradicted", "unsupported_claim"
        return "unknown", None
    if issue == "unsupported_claim":
        abnormal = shipment.get("late") or verdict in {"duplicate_capture", "capture_mismatch"}
        return ("unknown" if abnormal else "supported"), None
    return "unknown", None


def verify(
    output: dict[str, Any], ledger: EvidenceLedger, facts: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """Mutates ``output`` with safe repairs. Returns (repairs, unrecoverable_errors)."""
    repairs: list[str] = []
    errors: list[str] = []
    known = ledger.all_refs()

    # provenance: only refs this case's ledger actually received
    def scoped(refs: list[str]) -> list[str]:
        kept = list(dict.fromkeys(r for r in refs if r in known))
        if len(kept) != len(refs):
            repairs.append("evidence_scope")
        return kept[:30]

    output["evidence_refs"] = scoped(output["evidence_refs"])
    for claim in output.get("claim_assessments", []):
        claim["evidence_refs"] = scoped(claim["evidence_refs"])

    financial = output["financial_resolution"]
    status = output["assessment"]["case_status"]
    payment = facts.get("payment", {})
    refund = Decimal(str(financial["recommended_refund_brl"]))

    # money: never above what the authoritative timeline shows as still refundable
    if refund > 0:
        if not payment.get("timeline_available") or payment.get("captured") is None:
            refund = Decimal(0)
            repairs.append("refund_without_timeline")
        else:
            ceiling = max(payment["captured"] - (payment.get("refunded") or 0), Decimal(0))
            if refund - ceiling > CENT:
                refund = ceiling
                repairs.append("refund_capped_to_timeline")
    if status == "no_action" and refund > 0:
        refund = Decimal(0)
        repairs.append("no_action_refund")
    if refund != Decimal(str(financial["recommended_refund_brl"])):
        financial["recommended_refund_brl"] = money(refund)
        for line in financial["refund_lines"][:1]:
            line["amount_brl"] = money(refund)
        financial["refund_lines"] = financial["refund_lines"][:1] if refund > 0 else []
        output["payment_analysis"]["refundable_total_brl"] = money(refund)
    line_total = sum(
        (Decimal(str(line["amount_brl"])) for line in financial["refund_lines"]), Decimal(0)
    )
    recommended = Decimal(str(financial["recommended_refund_brl"]))
    if line_total.quantize(CENT) != recommended.quantize(CENT):
        errors.append("refund_total")

    # seller responsibility must name the seller(s)
    sellers = output["affected_entities"]["seller_ids"]
    parties = output["root_cause_analysis"]["responsible_parties"]
    if any(p["party_type"] == "seller" and not p["party_id"] for p in parties):
        if sellers:
            parties[:] = [p for p in parties if p["party_type"] != "seller"] + [
                {"party_type": "seller", "party_id": s} for s in sellers
            ]
            del parties[5:]
            repairs.append("seller_party_ids")
        else:
            errors.append("seller_without_id")

    entity = output["entity_resolution"]
    if entity["status"] == "resolved" and not output["affected_entities"]["order_ids"]:
        errors.append("resolved_without_order")
    actions = output["resolution_actions"]
    output["resolution_actions"] = list(dict.fromkeys(actions))[:8]
    confidence = output["assessment"]["confidence"]
    if not 0 <= confidence <= 1:
        errors.append("confidence_bounds")
    return repairs, errors
