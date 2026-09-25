"""Case-scoped MCP access: permission check, discovery-driven arguments, cache, retry,
call budget and provenance ledger. This is the only path from an agent to the gateway."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..mcp_gateway import EvidenceGateway, MCPToolError, ToolSpec
from ..trace import TraceWriter

# Least-privilege tool ownership. Discovery can only *narrow* this (tool missing on the
# server) — it never widens what an agent may call.
AGENT_TOOL_PERMISSIONS: dict[str, frozenset[str]] = {
    "entity-customer-agent": frozenset({"get_order", "get_customer_history"}),
    "order-item-agent": frozenset({"get_order_items", "get_product_context", "get_sellers"}),
    "shipment-agent": frozenset({"get_shipment_summary"}),
    "payment-refund-agent": frozenset(
        {"get_order_payments", "get_payment_timeline", "get_refund_timeline"}
    ),
    "policy-agent": frozenset({"get_policy"}),
}

# Canonical argument -> accepted spellings, used only when the discovered input schema
# does not list the canonical name.
ARGUMENT_ALIASES: dict[str, tuple[str, ...]] = {
    "order_id": ("order_id", "orderId", "order"),
    # not `customer_id`: in Olist that is the per-order ID, a different entity
    "customer_unique_id": ("customer_unique_id", "customerUniqueId", "customer"),
    "policy_version": ("policy_version", "policyVersion", "version"),
}

MAX_ATTEMPTS = 2  # one retry, transport failures only
ATTEMPT_TIMEOUT_S = 45.0
DEFAULT_CALL_BUDGET = 10


@dataclass(frozen=True)
class Evidence:
    tool: str
    actor: str
    ref: str
    domain: str
    data: Any
    warnings: tuple[str, ...] = ()


@dataclass
class EvidenceLedger:
    case_id: str
    gateway: EvidenceGateway
    trace: TraceWriter
    specs: dict[str, ToolSpec]
    call_budget: int = DEFAULT_CALL_BUDGET
    sink: Callable[[str, str, dict[str, Any], dict[str, Any] | None], None] | None = None
    cache: dict[tuple[str, tuple[tuple[str, str], ...]], Evidence | None] = field(
        default_factory=dict
    )
    failures: dict[str, str] = field(default_factory=dict)
    audited_calls: int = 0

    def available(self, tool_name: str) -> bool:
        return tool_name in self.specs

    def _adapt(self, spec: ToolSpec, arguments: dict[str, str]) -> dict[str, str] | None:
        properties = spec.properties - {"case_id"}
        if not properties:  # schema not published: pass canonical names through
            return dict(arguments)
        adapted: dict[str, str] = {}
        for canonical, value in arguments.items():
            name = next(
                (alias for alias in ARGUMENT_ALIASES.get(canonical, (canonical,))
                 if alias in properties),
                None,
            )
            if name is not None:
                adapted[name] = value
        missing = (spec.required - {"case_id"}) - set(adapted)
        return None if missing else adapted

    async def call(self, actor: str, tool_name: str, **arguments: str) -> Evidence | None:
        if tool_name not in AGENT_TOOL_PERMISSIONS.get(actor, frozenset()):
            raise PermissionError(f"actor {actor!r} is not permitted to call {tool_name!r}")
        spec = self.specs.get(tool_name)
        if spec is None:
            self.failures[tool_name] = "tool_not_discovered"
            return None
        adapted = self._adapt(spec, arguments)
        if adapted is None:
            self.failures[tool_name] = "argument_not_in_schema"
            return None
        key = (tool_name, tuple(sorted(adapted.items())))
        if key in self.cache:
            return self.cache[key]
        if self.audited_calls >= self.call_budget:
            self.failures[tool_name] = "call_budget_exhausted"
            return None

        envelope: dict[str, Any] | None = None
        for attempt in range(MAX_ATTEMPTS):
            self.audited_calls += 1
            try:
                envelope = await asyncio.wait_for(
                    self.gateway.call(tool_name, case_id=self.case_id, **adapted),
                    timeout=ATTEMPT_TIMEOUT_S,
                )
                self.failures.pop(tool_name, None)
                break
            except MCPToolError:
                self.failures[tool_name] = "tool_rejected"  # business error: never retry
                break
            except (TimeoutError, ConnectionError, OSError):
                self.failures[tool_name] = "transient_failure"
                if attempt + 1 >= MAX_ATTEMPTS or self.audited_calls >= self.call_budget:
                    break
            except (RuntimeError, ValueError):
                self.failures[tool_name] = "invalid_envelope"
                break

        if self.sink is not None:
            self.sink(self.case_id, tool_name, adapted, envelope)
        evidence = self._record(actor, tool_name, envelope)
        self.cache[key] = evidence
        return evidence

    def _record(
        self, actor: str, tool_name: str, envelope: dict[str, Any] | None
    ) -> Evidence | None:
        if not envelope:
            return None
        ref = envelope.get("evidence_ref")
        if not isinstance(ref, str):
            return None
        warnings = tuple(w for w in envelope.get("warnings") or () if isinstance(w, str))
        evidence = Evidence(
            tool=tool_name,
            actor=actor,
            ref=ref,  # stored verbatim: never edited, re-hashed or synthesised
            domain=str(envelope.get("domain", "")),
            data=envelope.get("data"),
            warnings=warnings,
        )
        self.trace.emit(
            case_id=self.case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool_name,
            evidence_refs=[ref],
            attributes={"domain": evidence.domain, "warning_count": len(warnings)},
        )
        return evidence

    # ---- read side -------------------------------------------------------------------
    def get(self, tool_name: str) -> Evidence | None:
        for (cached_tool, _), evidence in self.cache.items():
            if cached_tool == tool_name and evidence is not None:
                return evidence
        return None

    def data(self, tool_name: str) -> Any:
        evidence = self.get(tool_name)
        return evidence.data if evidence else None

    def refs(self, *tool_names: str) -> list[str]:
        wanted = set(tool_names) if tool_names else None
        result: list[str] = []
        for (tool_name, _), evidence in self.cache.items():
            if evidence is None or (wanted is not None and tool_name not in wanted):
                continue
            if evidence.ref not in result:
                result.append(evidence.ref)
        return result

    def all_refs(self) -> set[str]:
        return set(self.refs())

    def warnings(self) -> list[str]:
        return [w for evidence in self.cache.values() if evidence for w in evidence.warnings]
