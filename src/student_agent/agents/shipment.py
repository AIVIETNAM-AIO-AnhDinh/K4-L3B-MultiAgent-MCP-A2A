"""Shipment agent: delivery timeline, lateness and who caused it."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .a2a import A2AResult, A2ATask
from .base import SpecialistAgent
from .ledger import EvidenceLedger
from .parsing import (
    case_window,
    contains_any,
    event_name,
    event_status,
    parse_time,
    pick,
    scoped_events,
    seconds_between,
    unique,
)

DELIVERED_KEYS = (
    "delivered_customer_at", "order_delivered_customer_date", "delivered_at",
    "delivery_date", "delivered_customer_date",
)
ESTIMATED_KEYS = (
    "estimated_delivery_at", "order_estimated_delivery_date", "estimated_delivery_date",
    "promised_delivery_at", "estimated_at",
)
CARRIER_KEYS = (
    "delivered_carrier_at", "order_delivered_carrier_date", "carrier_handoff_at",
    "handed_to_carrier_at", "shipped_at", "delivered_carrier_date",
)
LIMIT_KEYS = ("shipping_limit_at", "shipping_limit_date", "seller_ship_by")
SELLER_ACTORS = ("seller",)
LOGISTICS_ACTORS = ("logistics", "carrier", "courier", "3pl", "transport")
FAILED_STATUSES = ("failed", "rejected", "canceled", "cancelled", "void", "superseded", "retracted")


def _date(value: Any) -> datetime | None:
    return parse_time(value) if isinstance(value, str) else None


def _late(actual: datetime | None, limit: datetime | None) -> bool | None:
    if actual is None or limit is None:
        return None
    if limit.time() == datetime.min.time():  # day-granular promise (Olist estimated date)
        return actual.date() > limit.date()
    delta = seconds_between(actual, limit)
    return None if delta is None else delta > 0


class ShipmentAgent(SpecialistAgent):
    name = "shipment-agent"

    async def handle(self, task: A2ATask, ledger: EvidenceLedger) -> A2AResult:
        order_id = task.payload["order_id"]
        order = task.payload.get("order") or {}
        window = case_window(
            parse_time(task.payload.get("opened_at")), parse_time(task.payload.get("purchased_at"))
        )
        evidence = await ledger.call(self.name, "get_shipment_summary", order_id=order_id)
        if evidence is None:
            facts = {"verdict": "insufficient_evidence", "timeline_complete": False,
                     "late": None, "attribution": None, "shipment_ids": [], "available": False}
            return self.result(task, "SHIPMENT_UNAVAILABLE", facts, [])

        data = evidence.data
        summary = data if isinstance(data, dict) else {}
        events, dropped = scoped_events(
            data, order_id=order_id, window=window, keys=("events", "timeline", "tracking")
        )
        events = [e for e in events if not contains_any(event_status(e), FAILED_STATUSES)]

        delivered = _date(pick(summary, *DELIVERED_KEYS) or pick(order, *DELIVERED_KEYS))
        estimated = _date(pick(summary, *ESTIMATED_KEYS) or pick(order, *ESTIMATED_KEYS))
        carrier = _date(pick(summary, *CARRIER_KEYS) or pick(order, *CARRIER_KEYS))
        limits = [parse_time(v) for v in task.payload.get("shipping_limits", [])]
        limit = _date(pick(summary, *LIMIT_KEYS)) or max(
            (value for value in limits if value), default=None
        )

        names = [event_name(e) for e in events]
        actors = [str(pick(e, "actor", "responsible_party", "party", "source") or "").lower()
                  for e in events]
        summary_late = _late(delivered, estimated)
        event_late = any("late" in n or "delay" in n for n in names)
        lost = any("lost" in n for n in names) or str(pick(summary, "status") or "") == "lost"
        returned = any("return" in n for n in names)

        # Who delayed? explicit event actor > seller handoff vs shipping limit
        delay_actor = None
        for name, actor in zip(names, actors, strict=True):
            if "late" in name or "delay" in name:
                if contains_any(actor, SELLER_ACTORS) or "seller" in name:
                    delay_actor = "seller"
                elif contains_any(actor, LOGISTICS_ACTORS) or contains_any(name, LOGISTICS_ACTORS):
                    delay_actor = "logistics_provider"
        handoff_late = _late(carrier, limit)
        attribution = delay_actor or (
            "seller" if handoff_late else ("logistics_provider" if handoff_late is False else None)
        )

        conflicts: list[dict[str, Any]] = []
        if event_late and summary_late is False:
            conflicts.append({
                "field": "delivery_timeliness",
                "sources": ["get_shipment_summary.summary", "get_shipment_summary.events"],
                "selected_source": "get_shipment_summary.events",
                "resolution_code": "AUTHORITATIVE_EVENT",
            })
        if delay_actor and handoff_late is not None and (
            (delay_actor == "seller") != bool(handoff_late)
        ):
            conflicts.append({
                "field": "delay_responsibility",
                "sources": ["get_shipment_summary.events", "get_order_items.shipping_limit"],
                "selected_source": "get_shipment_summary.events",
                "resolution_code": "AUTHORITATIVE_EVENT",
            })

        late = True if event_late else summary_late
        if lost:
            verdict = "lost"
        elif returned:
            verdict = "returned"
        elif late:
            verdict = {"seller": "seller_delay", "logistics_provider": "logistics_delay"}.get(
                attribution or "", "logistics_delay"
            )
        elif late is False:
            verdict = "on_time"
        else:
            verdict = "insufficient_evidence"

        shipment_ids = unique(
            [pick(summary, "shipment_id", "tracking_id", "tracking_code")]
            + [pick(e, "shipment_id", "tracking_id") for e in events]
        )
        facts = {
            "available": True,
            "verdict": verdict,
            "late": late,
            "attribution": attribution if late else None,
            "timeline_complete": bool(delivered and estimated) or bool(events),
            "shipment_ids": shipment_ids,
            "dropped_events": dropped,
        }
        return self.result(task, f"SHIPMENT_{verdict.upper()}", facts,
                           ledger.refs("get_shipment_summary"), conflicts)
