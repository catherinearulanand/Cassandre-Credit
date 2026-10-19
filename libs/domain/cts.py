"""Credit Term Sheet (CTS): the machine-executable sanction. The most important contract.

A published version is immutable. Amendments create version n+1 with ``supersedes_version``.
Every extracted field carries citations; a typed field carries a HumanEntry instead.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from platform_core.canonical import content_hash
from platform_core.money import Money
from pydantic import BaseModel, ConfigDict, Field, model_validator

from domain.lineage import Citation, HumanEntry


class FacilityType(StrEnum):
    TERM_LOAN = "TL"
    CASH_CREDIT = "CC"
    OVERDRAFT = "OD"
    WCDL = "WCDL"
    LETTER_OF_CREDIT = "LC"
    BANK_GUARANTEE = "BG"
    BILL_DISCOUNTING = "BD"
    ECB = "ECB"
    SYNDICATED_TRANCHE = "SYND"

    @property
    def revolving(self) -> bool:
        return self in (FacilityType.CASH_CREDIT, FacilityType.OVERDRAFT)

    @property
    def fund_based(self) -> bool:
        return self not in (FacilityType.LETTER_OF_CREDIT, FacilityType.BANK_GUARANTEE)


class Lineaged(BaseModel):
    """Mixin: a CTS element must have citations or a human entry (Section 7.3)."""

    citations: list[Citation] = []
    human_entry: HumanEntry | None = None

    @model_validator(mode="after")
    def _lineage(self) -> Lineaged:
        if not self.citations and self.human_entry is None:
            raise ValueError(f"{type(self).__name__} requires citations or a human_entry")
        return self


class RepaymentInstalment(BaseModel):
    model_config = ConfigDict(frozen=True)
    due_date: date
    principal: Money


class Facility(Lineaged):
    facility_id: str
    facility_type: FacilityType
    amount: Money
    tenor_months: int = Field(ge=1, le=360)
    pricing_spread_bps: int = Field(ge=0, le=2000)
    benchmark: str = "EBLR"
    repayment_schedule: list[RepaymentInstalment] = []
    margin_pct: Decimal | None = None  # working capital stock/book-debt margin
    receivable_age_cutoff_days: int | None = None


class LimitNode(Lineaged):
    node_id: str
    parent_id: str | None
    node_type: Literal[
        "group", "obligor", "facility", "sub_limit", "product", "currency", "tenor_bucket"
    ]
    facility_id: str | None = None
    sanctioned: Money
    interchangeable_with: list[str] = []
    seasonal_schedule: list[dict[str, str]] = []
    valid_from: date
    valid_to: date


class CollateralRequirement(Lineaged):
    collateral_id: str
    collateral_type: Literal[
        "stock",
        "book_debts",
        "immovable_property",
        "plant_machinery",
        "listed_shares",
        "mutual_funds",
        "fixed_deposit",
        "guarantee",
        "gold",
    ]
    charge_type: Literal["hypothecation", "mortgage", "pledge", "lien", "assignment", "guarantee"]
    rank: Literal["first", "second", "pari_passu", "subservient", "exclusive"]
    margin_pct: Decimal = Decimal("0.25")
    haircut_pct: Decimal = Decimal("0")
    secures_facility_ids: list[str]
    registrations_required: list[
        Literal["CERSAI", "CHG-1", "NeSL", "insurance_bank_clause", "title_search", "valuation"]
    ] = []


Operator = Literal[">=", "<=", ">", "<", "=="]


class CovenantDef(Lineaged):
    covenant_id: str
    klass: Literal["financial", "information", "affirmative", "negative", "esg"]
    name: str
    formula: str | None = None
    operator: Operator | None = None
    threshold: Decimal | None = None
    test_frequency: Literal["quarterly", "half_yearly", "annual", "monthly", "event"]
    test_basis: Literal["standalone", "consolidated"] = "standalone"
    first_test_date: date | None = None
    cure_period_days: int = Field(default=0, ge=0)
    grace_days: int = Field(default=0, ge=0)
    equity_cure_allowed: bool = False
    max_equity_cures: int = 0
    consequence: list[str] = []
    applies_to_limit_ids: list[str] = []
    rounding_dp: int = 2

    @model_validator(mode="after")
    def _financial_needs_formula(self) -> CovenantDef:
        if self.klass == "financial" and (
            self.formula is None or self.operator is None or self.threshold is None
        ):
            raise ValueError(
                f"financial covenant {self.covenant_id} needs formula, operator, threshold"
            )
        return self


class Condition(Lineaged):
    condition_id: str
    kind: Literal["precedent", "subsequent"]
    description: str
    evidence_doc_type: str
    due_date: date | None = None
    blocks: list[str] = []  # facility ids, "first_drawdown" or "all_drawdowns"
    stops_drawdown_when_overdue: bool = False


class Trigger(BaseModel):
    kind: Literal["rating_linked_pricing", "step_up", "reset"]
    detail: dict[str, str]


class CreditTermSheet(BaseModel):
    cts_id: str
    version: int = Field(ge=1)
    obligor_id: str
    group_id: str | None = None
    sanction_ref: str
    sanctioning_authority: str
    sanctioning_authority_power: Money
    sanction_date: date
    valid_until: date
    review_due: date
    facilities: list[Facility]
    limits: list[LimitNode]
    collateral: list[CollateralRequirement] = []
    covenants: list[CovenantDef] = []
    definitions: dict[str, str] = {}
    conditions: list[Condition] = []
    triggers: list[Trigger] = []
    supersedes_version: int | None = None
    amendment_sanction_ref: str | None = None
    content_hash: str = ""

    @model_validator(mode="after")
    def _structure(self) -> CreditTermSheet:
        fac_ids = {f.facility_id for f in self.facilities}
        if len(fac_ids) != len(self.facilities):
            raise ValueError("duplicate facility ids")
        node_ids = {n.node_id for n in self.limits}
        for n in self.limits:
            if n.parent_id is not None and n.parent_id not in node_ids:
                raise ValueError(f"limit {n.node_id} parent {n.parent_id} missing")
            if n.facility_id is not None and n.facility_id not in fac_ids:
                raise ValueError(f"limit {n.node_id} references unknown facility")
        for c in self.covenants:
            for lid in c.applies_to_limit_ids:
                if lid not in node_ids:
                    raise ValueError(f"covenant {c.covenant_id} references unknown limit {lid}")
        if self.version > 1 and (
            self.supersedes_version != self.version - 1 or not self.amendment_sanction_ref
        ):
            raise ValueError(
                "amended CTS needs supersedes_version = version-1 and an amendment_sanction_ref"
            )
        if self.valid_until <= self.sanction_date:
            raise ValueError("valid_until must be after sanction_date")
        return self

    def compute_hash(self) -> str:
        return content_hash(self.model_dump(mode="python", exclude={"content_hash"}))

    def facility(self, facility_id: str) -> Facility:
        for f in self.facilities:
            if f.facility_id == facility_id:
                return f
        raise KeyError(facility_id)

    def total_sanctioned(self) -> Money:
        total = Money.zero()
        for f in self.facilities:
            total = total + f.amount
        return total
