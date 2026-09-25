"""Payment/refund agent: reconcile captures and refunds against the authoritative timeline."""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import Any

from .a2a import A2AResult, A2ATask
from .base import SpecialistAgent
from .ledger import EvidenceLedger
from .parsing import (
    CENT,
    case_window,
    contains_any,
    decimal,
    event_amount,
    event_name,
    event_status,
    listing,
    money,
    parse_time,
    pick,
    scoped_events,
    unique,
)

CAPTURE_WORDS = ("captur", "charge", "settle", "paid", "payment_approved", "debit")
NOT_CAPTURE_WORDS = ("refund", "revers", "void", "chargeback", "authoriz", "fail", "declin",
                     "cancel", "pending")
BAD_STATUSES = ("fail", "declin", "cancel", "void", "revers", "error", "superseded", "retracted")
REFUND_DONE = ("complet", "succe", "confirm", "refunded", "settled", "processed", "paid", "done")
REFUND_PENDING = ("pending", "request", "initiat", "processing", "in_progress", "queued",
                  "submitted", "awaiting", "open", "created")
REFUND_FAILED = ("fail", "reject", "declin", "error", "revers", "bounced", "returned")
PAYMENT_KEYS = ("payments", "order_payments", "records", "items")


def _is_capture(event: dict[str, Any]) -> bool:
    name = event_name(event)
    status = event_status(event)
    if not contains_any(name, CAPTURE_WORDS) or contains_any(name, NOT_CAPTURE_WORDS):
        return False
    return not contains_any(status, BAD_STATUSES)


def _classify(text: str) -> str | None:
    if contains_any(text, REFUND_FAILED):
        return "failed"
    if contains_any(text, REFUND_PENDING):
        return "pending"
    if contains_any(text, REFUND_DONE):
        return "completed"
    return None


def _refund_state(event: dict[str, Any]) -> str | None:
    """Event name is more specific than its generic status (`refund_requested` + `confirmed`
    is still pending), so classify by name first and fall back to status."""
    raw = event_name(event)
    if raw.endswith("refunded"):
        return "completed"
    name = raw.replace("refund", "")
    return _classify(name) or _classify(event_status(event))


class PaymentRefundAgent(SpecialistAgent):
    name = "payment-refund-agent"

    async def handle(self, task: A2ATask, ledger: EvidenceLedger) -> A2AResult:
        payload = task.payload
        order_id = payload["order_id"]
        window = case_window(
            parse_time(payload.get("opened_at")), parse_time(payload.get("purchased_at"))
        )
        conflicts: list[dict[str, Any]] = []

        timeline = await ledger.call(self.name, "get_payment_timeline", order_id=order_id)
        snapshot = None
        if payload.get("need_payment_snapshot"):
            snapshot = await ledger.call(self.name, "get_order_payments", order_id=order_id)
        refund = None
        if payload.get("need_refund_timeline"):
            refund = await ledger.call(self.name, "get_refund_timeline", order_id=order_id)

        # --- captures: authoritative timeline events, scoped to order + case window -------
        events, dropped = scoped_events(
            timeline.data if timeline else None, order_id=order_id, window=window
        )
        captures = [e for e in events if _is_capture(e)]
        capture_amounts = [event_amount(e) or Decimal(0) for e in captures]
        captured = sum(capture_amounts, Decimal(0))

        # --- snapshot (order_payments): Olist columns, may be stale/duplicated ------------
        payments = listing(snapshot.data if snapshot else None, *PAYMENT_KEYS)
        payments = [p for p in payments if pick(p, "order_id") in (None, "", order_id)]
        snapshot_total = sum(
            (decimal(pick(p, "payment_value", "amount_brl", "amount")) or Decimal(0))
            for p in payments
        )
        if timeline is None and payments:
            captured = snapshot_total  # degraded: no authoritative source
        elif payments and captures and abs(snapshot_total - captured) > CENT:
            conflicts.append({
                "field": "captured_total_brl",
                "sources": ["get_order_payments", "get_payment_timeline"],
                "selected_source": "get_payment_timeline",
                "resolution_code": "AUTHORITATIVE_EVENT",
            })

        # duplicate capture = same amount captured more than once (optionally same reference)
        keyed = Counter(
            (str(pick(e, "payment_reference", "payment_id", "transaction_id") or ""),
             (event_amount(e) or Decimal(0)).quantize(CENT))
            for e in captures
        )
        by_amount = Counter(a.quantize(CENT) for a in capture_amounts)
        duplicate_amount = sum(
            (amount * (count - 1) for (_, amount), count in keyed.items() if count > 1),
            Decimal(0),
        ) or sum((amount * (count - 1) for amount, count in by_amount.items() if count > 1),
                 Decimal(0))

        # --- refunds: refund timeline first, then refund events inside payment timeline ---
        refund_events, _ = scoped_events(
            refund.data if refund else None, order_id=order_id, window=window
        )
        refund_events += [e for e in events if "refund" in event_name(e)]
        latest: dict[str, tuple[str, Decimal]] = {}
        refund_amounts: list[Decimal] = []
        for index, event in enumerate(refund_events):
            state = _refund_state(event)
            if state is None:
                continue
            key = str(pick(event, "refund_id", "refund_reference", "payment_reference") or index)
            amount = event_amount(event) or (latest.get(key, ("", Decimal(0)))[1])
            latest[key] = (state, amount)  # events are chronological: last state wins
            refund_amounts.append(amount)
        refunded = sum((a for s, a in latest.values() if s == "completed"), Decimal(0))
        pending = sum((a for s, a in latest.values() if s == "pending"), Decimal(0))
        failed = sum((a for s, a in latest.values() if s == "failed"), Decimal(0))
        states = {s for s, _ in latest.values()}

        expected = decimal(payload.get("expected_total_brl"))
        mismatch = (
            expected is not None and captured > 0 and abs(captured - expected) > CENT
            and not duplicate_amount
        )
        split = len(payments) > 1 or len(captures) > 1
        types = unique(pick(p, "payment_type", "method") for p in payments)

        if timeline is None and snapshot is None and refund is None:
            verdict = "insufficient_evidence"
        elif "failed" in states:
            verdict = "refund_failed"
        elif "pending" in states:
            verdict = "refund_pending"
        elif duplicate_amount:
            verdict = "duplicate_capture"
        elif mismatch:
            verdict = "capture_mismatch"
        elif captured > 0 and refunded >= captured:
            verdict = "refunded"
        elif captured > 0 or payments:
            verdict = "reconciled"
        else:
            verdict = "insufficient_evidence"

        payment_references = unique(
            [pick(p, "payment_reference", "payment_id", "transaction_id") for p in payments]
            + [pick(e, "payment_reference", "payment_id", "transaction_id") for e in captures]
        )
        facts = {
            "available": timeline is not None or snapshot is not None,
            "verdict": verdict,
            "captured_total_brl": money(captured) if (captures or payments) else None,
            "captured": captured,
            "snapshot_total": snapshot_total if payments else None,
            "capture_count": len(captures),
            "payment_count": len(payments),
            "payment_types": types,
            "split": split,
            "duplicate_amount": duplicate_amount,
            "mismatch_amount": (captured - expected) if mismatch and expected is not None
            else Decimal(0),
            "refunded": refunded,
            "refund_pending": pending,
            "refund_failed": failed,
            "refund_states": sorted(states),
            "refund_source": refund is not None or bool(refund_amounts),
            "timeline_available": timeline is not None,
            "payment_references": payment_references,
            "dropped_events": dropped,
        }
        refs = ledger.refs("get_payment_timeline", "get_order_payments", "get_refund_timeline")
        return self.result(task, f"PAYMENT_{verdict.upper()}", facts, refs, conflicts)
