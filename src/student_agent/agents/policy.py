"""Policy agent: select the published rule for the issue and compute its financial effect.

The published policy (``get_policy``) is authoritative. ``DEFAULT_RULES`` only fills a field
the policy does not state, and every fallback is reported in the facts (`fallback_fields`).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .a2a import A2AResult, A2ATask
from .base import SpecialistAgent
from .ledger import EvidenceLedger
from .parsing import contains_any, decimal, pick

CASE_STATUSES = {"action_required", "no_action", "needs_investigation"}
PARTY_TYPES = {
    "seller", "platform", "logistics_provider", "payment_provider", "customer", "unknown",
}
RULE_KEY_FIELDS = ("issue", "primary_issue", "issue_code", "topic", "code", "rule_id", "case_type",
                   "name", "id")

# issue -> (case_status, actions, refund_basis, responsible party types)
DEFAULT_RULES: dict[str, tuple[str, list[str], str, list[str]]] = {
    "late_delivery_seller": ("action_required", ["refund_freight"], "freight", ["seller"]),
    "late_delivery_logistics": (
        "action_required", ["refund_freight"], "freight", ["logistics_provider"]
    ),
    "canceled_order_paid": ("action_required", ["refund_full_payment"], "full", ["platform"]),
    "unavailable_order_paid": ("action_required", ["refund_full_payment"], "full", ["seller"]),
    "valid_split_payment": ("no_action", ["close_case"], "none", []),
    "payment_mismatch": (
        "action_required", ["refund_overcharge"], "difference", ["payment_provider"]
    ),
    "duplicate_charge": (
        "action_required", ["refund_duplicate_charge"], "duplicate", ["payment_provider"]
    ),
    "refund_pending": ("action_required", ["escalate_refund"], "none", ["payment_provider"]),
    "refund_failed": ("action_required", ["retry_refund"], "failed_refund", ["payment_provider"]),
    "unsupported_claim": ("no_action", ["close_case"], "none", []),
    "insufficient_evidence": (
        "needs_investigation", ["manual_investigation"], "none", ["unknown"]
    ),
}


def find_rule(data: Any, issue: str) -> dict[str, Any] | None:
    """Locate the rule for ``issue`` in dict-keyed, list-based or nested policy documents."""
    stack: list[Any] = [data]
    seen = 0
    while stack and seen < 500:
        node = stack.pop(0)
        seen += 1
        if isinstance(node, dict):
            value = node.get(issue)
            if isinstance(value, dict):
                return value
            if any(node.get(key) == issue for key in RULE_KEY_FIELDS):
                return node
            if isinstance(node.get("issues"), list) and issue in node["issues"]:
                return node
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            stack.extend(v for v in node if isinstance(v, (dict, list)))
    return None


def _basis(rule: dict[str, Any]) -> str | None:
    raw = pick(rule, "refund_basis", "refund_rule", "refund_policy", "refund_type", "refund",
               "compensation", "refund_method", "refund_scope")
    if isinstance(raw, dict):
        raw = pick(raw, "basis", "type", "scope", "amount")
    if raw is None:
        return None
    if isinstance(raw, bool):
        return "full" if raw else "none"
    if isinstance(raw, (int, float)) or decimal(raw) is not None:
        return f"amount:{raw}"
    text = str(raw).lower()
    if contains_any(text, ("none", "no_refund", "not_eligible", "no refund", "zero")):
        return "none"
    if "freight" in text or "shipping" in text:
        return "freight"
    if "duplicate" in text:
        return "duplicate"
    if contains_any(text, ("difference", "mismatch", "excess", "over", "delta")):
        return "difference"
    if "fail" in text or "retry" in text:
        return "failed_refund"
    if "pending" in text:
        return "none"
    if contains_any(text, ("full", "captur", "total", "paid", "order_value", "remaining")):
        return "full"
    if contains_any(text, ("item", "price", "product")):
        return "items"
    return None


def _parties(rule: dict[str, Any]) -> list[dict[str, Any]] | None:
    raw = pick(rule, "responsible_parties", "responsible_party", "liable_parties", "liable_party",
               "responsibility", "responsible")
    if raw is None:
        return None
    result: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else [raw]:
        if isinstance(item, str) and item in PARTY_TYPES:
            result.append({"party_type": item, "party_id": None})
        elif isinstance(item, dict):
            party_type = pick(item, "party_type", "type", "party")
            if party_type in PARTY_TYPES:
                result.append({"party_type": party_type, "party_id": pick(item, "party_id", "id")})
    return result


def _actions(rule: dict[str, Any]) -> list[str] | None:
    raw = pick(rule, "resolution_actions", "recommended_actions", "actions", "recommended_action",
               "action", "resolution_action")
    items = raw if isinstance(raw, list) else [raw] if raw is not None else []
    actions = [str(a).strip() for a in items if isinstance(a, str) and a.strip()]
    return actions[:8] or None


def refund_amount(basis: str, facts: dict[str, Any]) -> Decimal:
    captured: Decimal = facts.get("captured") or Decimal(0)
    refunded: Decimal = facts.get("refunded") or Decimal(0)
    remaining = max(captured - refunded, Decimal(0))
    if basis.startswith("amount:"):
        return decimal(basis.split(":", 1)[1]) or Decimal(0)
    return {
        "none": Decimal(0),
        "freight": decimal(facts.get("freight_total_brl")) or Decimal(0),
        "items": decimal(facts.get("items_total_brl")) or Decimal(0),
        "full": max(remaining - (facts.get("refund_pending") or Decimal(0)), Decimal(0)),
        "duplicate": facts.get("duplicate_amount") or Decimal(0),
        "difference": max(facts.get("mismatch_amount") or Decimal(0), Decimal(0)),
        "failed_refund": facts.get("refund_failed") or remaining,
    }.get(basis, Decimal(0))


class PolicyAgent(SpecialistAgent):
    name = "policy-agent"

    async def handle(self, task: A2ATask, ledger: EvidenceLedger) -> A2AResult:
        payload = task.payload
        issue: str = payload["issue"]
        facts: dict[str, Any] = payload.get("facts", {})
        version = payload.get("policy_version")
        evidence = None
        if isinstance(version, str) and version:
            evidence = await ledger.call(self.name, "get_policy", policy_version=version)
        rule = find_rule(evidence.data, issue) if evidence else None
        rule = rule or {}
        default_status, default_actions, default_basis, default_parties = DEFAULT_RULES.get(
            issue, DEFAULT_RULES["insufficient_evidence"]
        )
        fallback: list[str] = []

        status = pick(rule, "case_status", "status", "default_status", "outcome")
        if status not in CASE_STATUSES:
            status = default_status
            fallback.append("case_status")
        actions = _actions(rule)
        if actions is None:
            actions = list(default_actions)
            fallback.append("actions")
        basis = _basis(rule)
        explicit = decimal(pick(rule, "refund_brl", "refund_amount_brl", "refund_amount"))
        if explicit is not None:
            basis = f"amount:{explicit}"
        if basis is None:
            basis = default_basis
            fallback.append("refund_basis")
        ratio = decimal(pick(rule, "refund_ratio", "refund_percent", "refund_pct"))
        parties = _parties(rule)
        if parties is None:
            parties = [{"party_type": p, "party_id": None} for p in default_parties]
            fallback.append("responsible_parties")

        amount = refund_amount(basis, facts)
        if ratio is not None and not basis.startswith("amount:"):
            amount = amount * (ratio / 100 if ratio > 1 else ratio)
        decision = {
            "found": evidence is not None,
            "rule_found": bool(rule),
            "case_status": status,
            "actions": actions,
            "refund_basis": basis,
            "refund_amount": amount,
            "responsible_parties": parties,
            "fallback_fields": fallback,
        }
        code = "POLICY_RULE_SELECTED" if rule else (
            "POLICY_DEFAULT_APPLIED" if evidence else "POLICY_UNAVAILABLE"
        )
        return self.result(task, code, decision, ledger.refs("get_policy"))

