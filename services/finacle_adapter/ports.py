"""Port used by every service that needs Finacle. Services depend on this, not on HTTP."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

CommandType = Literal["upsert_limit", "set_limit_status", "set_dp", "covenant_record", "disburse"]


@dataclass(frozen=True)
class FinacleCommand:
    command_id: str  # idempotency key
    command_type: CommandType
    target: str
    body: dict[str, Any]
    correlation_id: str
    approval_id: str | None = None  # required for limit-changing commands initiated by a decision


@dataclass
class CommandResult:
    command_id: str
    status: Literal["accepted", "rejected", "duplicate", "failed", "needs_reconciliation", "dead"]
    finacle_ref: str | None = None
    detail: str | None = None
    attempts: int = 0


@dataclass
class Drift:
    entity: str
    field_name: str
    platform: Any
    finacle: Any


@dataclass
class ReconciliationReport:
    checked: int = 0
    drifts: list[Drift] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.drifts


class FinacleCommandPort(Protocol):
    def submit(self, command: FinacleCommand) -> None: ...
