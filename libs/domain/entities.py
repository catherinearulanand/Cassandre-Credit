"""Other core entities: obligor, spreads, signals, cases, obligations, test results."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from domain.lineage import Citation


class Obligor(BaseModel):
    obligor_id: str
    cif: str
    legal_name: str
    pan: str
    cin: str | None = None
    lei: str | None = None
    gstins: list[str] = []
    nic_code: str
    region: str
    portfolio: str
    group_id: str | None = None
    internal_rating: str
    promoter_names: list[str] = []  # personal data: never logged, redacted before LLM calls


class SpreadLine(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    amount_paise: int
    citations: tuple[Citation, ...] = ()
    source_label: str | None = None
    mapping_confidence: float = 1.0


class Spread(BaseModel):
    spread_id: str
    obligor_id: str
    period_end: date
    period_months: Literal[3, 6, 12]
    basis: Literal["standalone", "consolidated"]
    audited: bool
    lines: dict[str, SpreadLine]
    validation_exceptions: list[str] = []
    signed_off_by: str | None = None
    source_document_ids: list[str] = []

    def value(self, code: str) -> int | None:
        line = self.lines.get(code)
        return None if line is None else line.amount_paise


class TestResult(StrEnum):
    COMPLIED = "COMPLIED"
    BREACHED = "BREACHED"
    WAIVED = "WAIVED"
    DEFERRED = "DEFERRED"
    IN_CURE = "IN_CURE"
    NOT_TESTED_MISSING_DATA = "NOT_TESTED_MISSING_DATA"
    NOT_COMPUTABLE = "NOT_COMPUTABLE"


class CovenantTestRecord(BaseModel):
    test_id: str
    covenant_id: str
    cts_id: str
    cts_version: int
    obligor_id: str
    period_end: date
    value: Decimal | None
    threshold: Decimal | None
    operator: str | None
    result: TestResult
    headroom_pct: Decimal | None = None
    inputs: dict[str, str] = {}  # variable -> amount (as string) used
    lineage: list[str] = []
    rule_versions: dict[str, str] = {}
    tested_at: datetime
    notes: list[str] = []


class SignalSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Signal(BaseModel):
    signal_id: str
    obligor_id: str
    source: str
    signal_type: str
    severity: SignalSeverity
    score: float = Field(ge=0, le=100)
    raised_at: datetime
    valid_at: date
    explanation: str
    evidence: list[str] = []
    rule_id: str | None = None
    rule_version: str | None = None


class CaseType(StrEnum):
    COVENANT_BREACH = "covenant_breach"
    EWS = "ews"
    RFA = "rfa"
    RESTRUCTURING = "restructuring"
    RECOVERY = "recovery"
    DOCUMENTATION_DEVIATION = "documentation_deviation"
    RECONCILIATION_DRIFT = "reconciliation_drift"
    PERFECTION_OVERDUE = "perfection_overdue"
    CLOSURE = "closure"
    SECURITY = "security"


class Obligation(BaseModel):
    obligation_id: str
    obligor_id: str
    cts_id: str
    doc_type: str
    period_end: date | None
    due_date: date
    status: Literal["open", "received", "overdue", "waived"] = "open"
    received_document_id: str | None = None
    chase_step: int = 0
