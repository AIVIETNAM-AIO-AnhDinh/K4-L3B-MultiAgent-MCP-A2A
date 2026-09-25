"""Defensive readers for MCP evidence payloads.

The MCP data contract only fixes the envelope (`mcp-evidence-response-v1`); the shape of
``data`` is tool-specific. Every reader here accepts the common Olist column names plus a
few synonyms, never raises on unexpected shapes and never invents values.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

CENT = Decimal("0.01")
ORDER_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$", re.IGNORECASE)

TIME_KEYS = (
    "event_at",
    "occurred_at",
    "timestamp",
    "at",
    "created_at",
    "updated_at",
    "processed_at",
    "event_time",
    "date",
)
AMOUNT_KEYS = ("amount_brl", "amount", "value_brl", "value", "payment_value", "refund_value")
EVENT_NAME_KEYS = ("event_type", "type", "event", "name", "action", "kind")


def records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def listing(data: Any, *keys: str) -> list[dict[str, Any]]:
    """Return the first list of records found under ``keys`` (or ``data`` itself)."""
    if isinstance(data, list):
        return records(data)
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return records(value)
    return []


def pick(record: Any, *keys: str) -> Any:
    if not isinstance(record, dict):
        return None
    for key in keys:
        value = record.get(key)
        if value is not None and value != "":
            return value
    return None


def unique(values: Iterable[Any], *, limit: int = 20) -> list[str]:
    result: list[str] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            continue
        normalized = str(value).strip()
        if normalized and normalized not in result and len(normalized) <= 128:
            result.append(normalized)
        if len(result) == limit:
            break
    return result


def decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, dict):
        value = pick(value, "amount_brl", "amount", "value")
        if value is None:
            return None
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() and result >= 0 else None


def money(value: Decimal | int | float | None) -> float:
    return float(Decimal(str(value or 0)).quantize(CENT))


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def seconds_between(a: datetime, b: datetime) -> float | None:
    """a - b in seconds; tolerant of naive/aware mixes (naive treated as case-local)."""
    try:
        return (a - b).total_seconds()
    except TypeError:
        try:
            return (a.replace(tzinfo=None) - b.replace(tzinfo=None)).total_seconds()
        except TypeError:
            return None


def record_time(record: dict[str, Any], keys: tuple[str, ...] = TIME_KEYS) -> datetime | None:
    for key in keys:
        parsed = parse_time(record.get(key))
        if parsed is not None:
            return parsed
    return None


def event_name(event: dict[str, Any]) -> str:
    return str(pick(event, *EVENT_NAME_KEYS) or "").strip().lower()


def event_status(event: dict[str, Any]) -> str:
    return str(pick(event, "status", "state", "result", "outcome") or "").strip().lower()


def event_amount(event: dict[str, Any]) -> Decimal | None:
    return decimal(pick(event, *AMOUNT_KEYS))


def closest(
    items: list[dict[str, Any]],
    anchor: datetime | None,
    keys: tuple[str, ...] = TIME_KEYS,
) -> dict[str, Any] | None:
    """Pick the record closest to the case time, preferring records not after it."""
    if not items:
        return None
    if anchor is None:
        return items[0]

    def rank(item: dict[str, Any]) -> tuple[int, float]:
        timestamp = record_time(item, keys)
        if timestamp is None:
            return (2, float("inf"))
        delta = seconds_between(anchor, timestamp)
        if delta is None:
            return (2, float("inf"))
        return (0 if delta >= 0 else 1, abs(delta))

    return min(items, key=rank)


WINDOW_DAYS = 120
PRE_PURCHASE_SLACK_S = 3 * 86400


def case_window(
    opened_at: datetime | None, purchased_at: datetime | None
) -> tuple[datetime | None, datetime | None]:
    """Business window of one order's lifecycle: from purchase (or opened_at - 120d) to
    opened_at + 120d. Historical events before the purchase are decoys."""
    if opened_at is None and purchased_at is None:
        return None, None
    end = (opened_at or purchased_at) + timedelta(days=WINDOW_DAYS)  # type: ignore[operator]
    if purchased_at is not None:
        start = purchased_at - timedelta(seconds=PRE_PURCHASE_SLACK_S)
    else:
        start = opened_at - timedelta(days=WINDOW_DAYS)  # type: ignore[operator]
    return start, end


def in_window(record: dict[str, Any], start: datetime | None, end: datetime | None) -> bool:
    timestamp = record_time(record)
    if timestamp is None:
        return True
    after = seconds_between(timestamp, start) if start else 0.0
    before = seconds_between(end, timestamp) if end else 0.0
    return (after is None or after >= 0) and (before is None or before >= 0)


def scoped_events(
    data: Any,
    *,
    order_id: str | None,
    window: tuple[datetime | None, datetime | None] = (None, None),
    keys: tuple[str, ...] = ("events", "timeline", "refunds", "payments", "items", "history"),
) -> tuple[list[dict[str, Any]], int]:
    """Events for this order inside its business window; returns (events, dropped_count).

    Events naming another order are always dropped. Only if *no* event falls inside the
    window (e.g. shifted synthetic clock) is the order-scoped list used unwindowed.
    """
    events = listing(data, *keys)
    scoped = [
        event
        for event in events
        if order_id is None or event.get("order_id") in (None, "", order_id)
    ]
    windowed = [event for event in scoped if in_window(event, *window)]
    selected = windowed or scoped
    return selected, len(events) - len(selected)


def contains_any(text: str, needles: Iterable[str]) -> bool:
    return any(needle in text for needle in needles)
