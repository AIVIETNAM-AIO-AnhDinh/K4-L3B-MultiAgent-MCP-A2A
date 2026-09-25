"""Entity/customer agent: resolve the affected order and the customer's context."""

from __future__ import annotations

from typing import Any

from .a2a import A2AResult, A2ATask
from .base import SpecialistAgent
from .ledger import EvidenceLedger
from .parsing import ORDER_ID_PATTERN, closest, listing, parse_time, pick, records, unique

MAX_CANDIDATES = 5
MAX_BLIND_LOOKUPS = 3  # get_order calls allowed when customer history cannot narrow candidates
ORDER_TIME_KEYS = ("order_purchase_timestamp", "purchase_timestamp", "purchased_at", "created_at")
HISTORY_KEYS = ("orders", "order_history", "history", "records")


def _order_record(data: Any, order_id: str) -> dict[str, Any] | None:
    for record in records(data) + listing(data, "orders"):
        if pick(record, "order_id", "id") == order_id:
            return record
    return None


class EntityCustomerAgent(SpecialistAgent):
    name = "entity-customer-agent"

    async def handle(self, task: A2ATask, ledger: EvidenceLedger) -> A2AResult:
        payload = task.payload
        claimed = payload.get("claimed_order_id")
        claimed = claimed.strip() if isinstance(claimed, str) and claimed.strip() else None
        hint = payload.get("customer_unique_id_hint")
        anchor = parse_time(payload.get("opened_at"))
        want_history = bool(payload.get("include_customer_history", True))

        candidates = unique(([claimed] if claimed else []) + list(payload.get("candidates", [])))
        candidates = candidates[:MAX_CANDIDATES]
        rejected: list[str] = []
        viable: list[str] = []
        for candidate in candidates:
            # Synthetic negative markers / malformed IDs are rejected without an audited call.
            # The claimed ID is always checked against MCP, whatever its format.
            if candidate != claimed and (
                candidate.lower().startswith("candidate-")
                or not ORDER_ID_PATTERN.fullmatch(candidate)
            ):
                rejected.append(candidate)
            else:
                viable.append(candidate)

        orders: dict[str, dict[str, Any]] = {}

        async def confirm(order_id: str) -> bool:
            evidence = await ledger.call(self.name, "get_order", order_id=order_id)
            record = _order_record(evidence.data, order_id) if evidence else None
            if record is not None:
                orders[order_id] = record
                return True
            return False

        # 1) claimed ID is the strongest hypothesis
        if claimed and claimed in viable:
            await confirm(claimed)

        # 2) customer history. In Olist, orders carry only the per-order `customer_id`; the
        #    person is `customer_unique_id` (one person -> many customer_id/orders). The case
        #    hint is the server-issued alias for that person, so it is tried first; a real
        #    `customer_unique_id` on the order record is the single fallback lookup.
        order_customer = pick(orders.get(claimed or "", {}), "customer_unique_id")
        lookups = unique([hint if isinstance(hint, str) else None, order_customer], limit=2)
        history: list[dict[str, Any]] = []
        customer_record: dict[str, Any] = {}
        customer_id = lookups[0] if lookups else None
        for lookup in lookups if want_history else []:
            evidence = await ledger.call(
                self.name, "get_customer_history", customer_unique_id=lookup
            )
            if evidence is None:
                continue
            data = evidence.data
            history = listing(data, *HISTORY_KEYS)
            customer_record = data if isinstance(data, dict) else {}
            customer_id = lookup
            if history or customer_record:
                break
        history_ids = unique([pick(order, "order_id", "id") for order in history], limit=50)

        # 3) remaining candidates: narrow with history (free) before spending get_order calls
        others = [c for c in viable if c != claimed]
        if claimed not in orders:
            in_history = [c for c in others if c in history_ids]
            to_check = in_history or (others[:MAX_BLIND_LOOKUPS] if not history_ids else [])
            for candidate in to_check:
                await confirm(candidate)
        resolved = [c for c in candidates if c in orders]
        for candidate in viable:
            if candidate not in resolved:
                rejected.append(candidate)

        # 4) decide
        conflicts: list[dict[str, Any]] = []
        if claimed in orders:
            selected, status = claimed, "resolved"
            confidence = 0.98 if (not history_ids or claimed in history_ids) else 0.85
            resolved = [claimed]
            rejected = [c for c in candidates if c != claimed]
        elif len(resolved) == 1:
            selected, status, confidence = resolved[0], "resolved", 0.88
        elif len(resolved) > 1:
            selected_record = closest(
                [dict(orders[c], order_id=c) for c in resolved], anchor, ORDER_TIME_KEYS
            )
            selected = str(selected_record["order_id"]) if selected_record else resolved[0]
            status, confidence = "ambiguous", 0.55
        else:
            selected, status, confidence = None, "not_found", 0.2

        if selected and history_ids and selected not in history_ids:
            conflicts.append(
                {
                    "field": "customer_order_link",
                    "sources": ["get_order", "get_customer_history"],
                    "selected_source": "get_order",
                    "resolution_code": "ORDER_SYSTEM_OF_RECORD",
                }
            )

        # 5) snapshot of the selected order closest to case time (history may hold stale copies)
        snapshots = [o for o in history if pick(o, "order_id", "id") == selected]
        history_snapshot = closest(snapshots, anchor, ORDER_TIME_KEYS) if snapshots else None
        if len(snapshots) > 1:
            conflicts.append(
                {
                    "field": "order_snapshot",
                    "sources": ["get_customer_history", "get_order"],
                    "selected_source": "get_customer_history",
                    "resolution_code": "CASE_TIME_PROXIMITY",
                }
            )
        order_record = orders.get(selected or "", {})
        if history_snapshot and order_record:
            status_a = pick(order_record, "order_status", "status")
            status_b = pick(history_snapshot, "order_status", "status")
            if status_a and status_b and status_a != status_b:
                conflicts.append(
                    {
                        "field": "order_status",
                        "sources": ["get_order", "get_customer_history"],
                        "selected_source": "get_order",
                        "resolution_code": "ORDER_SYSTEM_OF_RECORD",
                    }
                )

        customer_unique_id = (
            pick(customer_record, "customer_unique_id")
            or next((pick(o, "customer_unique_id") for o in history
                     if pick(o, "customer_unique_id")), None)
            or (pick(order_record, "customer_unique_id") if order_record else None)
            or customer_id
        )
        facts = {
            "status": status,
            "selected_order_id": selected,
            "resolved_order_ids": resolved,
            "rejected_candidates": unique(rejected),
            "confidence": confidence,
            "order": order_record,
            "history_snapshot": history_snapshot or {},
            "customer_unique_id": customer_unique_id,
            "related_order_ids": history_ids[:20],
        }
        refs = ledger.refs("get_order", "get_customer_history")
        return self.result(task, f"ENTITY_{status.upper()}", facts, refs, conflicts)
