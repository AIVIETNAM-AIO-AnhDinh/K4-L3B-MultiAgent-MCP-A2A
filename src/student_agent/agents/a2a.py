"""Logical A2A envelopes between coordinator and specialists.

Envelopes are in-process only; the trace records their observable projection
(`task_assigned` / `handoff`) without payload content.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PROTOCOL = "day09-a2a-v1"
MAX_HOPS = 8


@dataclass(frozen=True)
class A2ATask:
    case_id: str
    sender: str
    recipient: str
    task: str
    hop: int
    payload: dict[str, Any] = field(default_factory=dict)
    protocol: str = PROTOCOL


@dataclass
class A2AResult:
    case_id: str
    sender: str
    task: str
    decision_code: str
    facts: dict[str, Any] = field(default_factory=dict)
    evidence_refs: list[str] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    protocol: str = PROTOCOL
