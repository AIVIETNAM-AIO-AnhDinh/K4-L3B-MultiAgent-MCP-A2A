"""Order/item agent: items, sellers and (only when needed) product context."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .a2a import A2AResult, A2ATask
from .base import SpecialistAgent
from .ledger import EvidenceLedger
from .parsing import decimal, listing, money, pick, unique

ITEM_KEYS = ("items", "order_items", "lines")


def _item_id(item: dict[str, Any]) -> str | None:
    """Prefer an explicit item ID; fall back to Olist's in-order `order_item_id` sequence."""
    value = pick(item, "item_id", "order_item_uid", "line_id", "order_item_id")
    return None if value is None else str(value)


class OrderItemAgent(SpecialistAgent):
    name = "order-item-agent"

    async def handle(self, task: A2ATask, ledger: EvidenceLedger) -> A2AResult:
        order_id = task.payload["order_id"]
        need_sellers = bool(task.payload.get("need_seller_detail"))
        need_product = bool(task.payload.get("need_product_context"))

        evidence = await ledger.call(self.name, "get_order_items", order_id=order_id)
        items = listing(evidence.data if evidence else None, *ITEM_KEYS)
        items = [i for i in items if pick(i, "order_id") in (None, "", order_id)]

        seller_ids = unique(pick(item, "seller_id") for item in items)
        if need_sellers or (items and not seller_ids):
            sellers = await ledger.call(self.name, "get_sellers", order_id=order_id)
            seller_ids = unique(
                seller_ids
                + [pick(s, "seller_id", "id") for s in listing(sellers.data if sellers else None,
                                                             "sellers", "items")]
            )
        product_ids = unique(pick(item, "product_id") for item in items)
        product_status: list[str] = []
        if need_product:
            product = await ledger.call(self.name, "get_product_context", order_id=order_id)
            for record in listing(product.data if product else None, "products", "items"):
                product_ids = unique(product_ids + [pick(record, "product_id", "id")])
                status = pick(record, "availability", "stock_status", "status", "product_status")
                if isinstance(status, str):
                    product_status.append(status.lower())

        price = sum((decimal(pick(i, "price", "item_price")) or Decimal(0)) for i in items)
        freight = sum(
            (decimal(pick(i, "freight_value", "freight", "shipping_fee")) or Decimal(0))
            for i in items
        )
        facts = {
            "items_found": bool(items),
            "item_ids": unique(_item_id(i) for i in items),
            "seller_ids": seller_ids,
            "product_ids": product_ids,
            "product_status": product_status,
            "shipping_limits": [
                str(v) for v in (pick(i, "shipping_limit_date", "shipping_limit_at") for i in items)
                if v
            ],
            "items_total_brl": money(price),
            "freight_total_brl": money(freight),
            "order_total_brl": money(price + freight) if items else None,
        }
        refs = ledger.refs("get_order_items", "get_sellers", "get_product_context")
        code = "ITEM_CONTEXT_READY" if items else "ITEM_CONTEXT_MISSING"
        return self.result(task, code, facts, refs)
