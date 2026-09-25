"""Entry point used by the CLI: one bounded coordinator/specialist run per case.

Design and invariants: see ARCHITECTURE.md. Agents live in ``student_agent.agents``.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from .agents import Coordinator, EvidenceLedger
from .agents.ledger import DEFAULT_CALL_BUDGET
from .mcp_gateway import EvidenceGateway, ToolSpec
from .trace import TraceWriter

EvidenceSink = Callable[[str, str, dict[str, Any], dict[str, Any] | None], None]
_sink: EvidenceSink | None = None


def set_evidence_sink(sink: EvidenceSink | None) -> None:
    """Optional local debug hook (CLI ``--dump-evidence``); never part of the submission."""
    global _sink
    _sink = sink


async def _discovered_specs(gateway: EvidenceGateway) -> dict[str, ToolSpec]:
    describe = getattr(gateway, "describe_tools", None)
    if describe is not None:
        return await describe()
    return {name: ToolSpec(name) for name in await gateway.list_tools()}


def _budget() -> int:
    try:
        return max(1, int(os.getenv("DAY09_CALL_BUDGET", DEFAULT_CALL_BUDGET)))
    except ValueError:
        return DEFAULT_CALL_BUDGET


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    case_id = case.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        raise ValueError("case must contain a non-empty case_id")
    specs = await _discovered_specs(gateway)  # cached by the gateway: one discovery per run
    ledger = EvidenceLedger(case_id, gateway, trace, specs, call_budget=_budget(), sink=_sink)
    return await Coordinator(case, ledger).run()
