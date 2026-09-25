from __future__ import annotations

from typing import Any

from .a2a import A2AResult, A2ATask
from .ledger import EvidenceLedger


class SpecialistAgent:
    """A specialist owns a set of tools, returns facts + refs and never builds output."""

    name = "specialist"

    async def handle(self, task: A2ATask, ledger: EvidenceLedger) -> A2AResult:
        raise NotImplementedError

    def result(
        self,
        task: A2ATask,
        decision_code: str,
        facts: dict[str, Any],
        refs: list[str],
        conflicts: list[dict[str, Any]] | None = None,
    ) -> A2AResult:
        return A2AResult(
            case_id=task.case_id,
            sender=self.name,
            task=task.task,
            decision_code=decision_code,
            facts=facts,
            evidence_refs=refs,
            conflicts=conflicts or [],
        )
