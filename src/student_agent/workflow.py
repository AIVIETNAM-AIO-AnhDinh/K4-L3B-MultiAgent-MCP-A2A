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


class PolicyAgent:
    def decide(self, investigation_data: dict, trace: TraceWriter, case_id: str) -> dict:
        """
        Nhận kết quả điều tra và đưa ra quyết định dựa trên Policy.
        """
        # --- 1. Viết Logic Rule-base ở đây ---
        primary_issue = investigation_data.get("inferred_issue", "unknown")
        
        # Ví dụ một số rule cơ bản:
        responsible_parties = []
        if primary_issue == "late_delivery_seller":
            responsible_parties = ["seller"]
        elif primary_issue == "late_delivery_logistics":
            responsible_parties = ["logistics_provider"]
        elif primary_issue == "payment_mismatch":
            responsible_parties = ["payment_provider"]
        else:
            responsible_parties = ["platform"] # Mặc định

        # Tính toán tiền hoàn (thường thì hoàn 100% giá trị đơn hàng nếu có lỗi giao hàng)
        order_value = investigation_data.get("order_value", 0.0)
        
        draft_decision = {
            "primary_issue": primary_issue,
            "responsible_parties": responsible_parties,
            "financial_resolution": {
                "recommended_refund_brl": order_value,
                "refund_lines": [
                    {"type": "item_refund", "amount": order_value}
                ]
            },
            "resolution_actions": ["issue_refund"]
        }

        # --- 2. Ghi nhận event bắt buộc ---
        trace.emit(
            case_id=case_id,
            event_type="policy_decided",
            actor="policy-agent",
            attributes={"decision_summary": f"Resolved issue {primary_issue} - Responsible: {responsible_parties}"}
        )
        
        return draft_decision


class VerifierAgent:
    def verify_and_calibrate(
        self, draft_decision: dict, investigation_data: dict, trace: TraceWriter, case_id: str
    ) -> dict:
        """
        Kiểm chứng chéo tính logic và chấm điểm confidence.
        """
        # --- 1. Cross-field Consistency (Kiểm tra mâu thuẫn) ---
        primary_issue = draft_decision["primary_issue"]
        responsible = draft_decision["responsible_parties"]
        
        if primary_issue == "late_delivery_seller" and "logistics_provider" in responsible:
            # Sửa lỗi tự động hoặc raise exception
            draft_decision["responsible_parties"] = ["seller"]
            
        # --- 2. Confidence Calibration ---
        # Điểm bắt đầu là 1.0 (100% tự tin)
        confidence = 1.0 
        
        # Nếu evidence không đầy đủ hoặc có conflict chưa giải quyết được, trừ điểm
        if investigation_data.get("has_unresolved_conflict", False):
            confidence -= 0.4
        if not investigation_data.get("has_full_evidence", True):
            confidence -= 0.2
            
        draft_decision["confidence"] = max(0.0, min(1.0, confidence)) # Đảm bảo nằm trong [0, 1]
        
        # Gom lại các evidence_refs từ Pha 3 đưa sang để xuất output cuối
        draft_decision["evidence_refs"] = investigation_data.get("evidence_refs", [])

        # --- 3. Ghi nhận lifecycle events ---
        trace.emit(
            case_id=case_id,
            event_type="verification_completed",
            actor="verifier-agent",
            attributes={"confidence": draft_decision["confidence"]}
        )
        
        trace.emit(
            case_id=case_id,
            event_type="case_finalized",
            actor="coordinator"
        )
        
        return draft_decision

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
