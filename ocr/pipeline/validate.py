"""Validation (stage 9): accounting identities decide what a human must see.

For each identity (BS sub-totals, assets = equity + liabilities, PBT, PAT, tax) the engine
computes the residual from extracted values. A zero residual with every component present is
strong independent evidence that those cells were read correctly. A non-zero residual flags the
cells involved.

Arithmetic-assisted repair: if a single cell, changed to the value that would close the
identity, sits within a small digit edit distance of what the OCR read, the repaired value is
*suggested* (``auto_corrected_from`` records the original). A repaired cell is never straight-
through: repairs speed up the human reviewer, they do not replace them. This also keeps repair
from silently undoing a deliberate edit (tampering), which would otherwise be "fixed" back to
the original figure and hidden.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from domain.coa import BS_IDENTITIES, PL_IDENTITIES
from identifiers.numbers import UnitScale

from ocr.pipeline.extract import StatementExtraction

# (total, [(component, sign)]) - total = sum(sign * component)
Identity = tuple[str, tuple[tuple[str, int], ...]]


def _plain(items: tuple[tuple[str, tuple[str, ...]], ...]) -> list[Identity]:
    return [(t, tuple((c, 1) for c in comps)) for t, comps in items]


IDENTITIES: dict[str, list[Identity]] = {
    "BS": [*_plain(BS_IDENTITIES), ("total_assets", (("total_equity_and_liabilities", 1),))],
    "PL": [
        *_plain(PL_IDENTITIES),
        ("pbt", (("total_income", 1), ("total_expenses", -1))),
        ("pat", (("pbt", 1), ("tax_expense", -1))),
    ],
}


@dataclass
class IdentityResult:
    total: str
    status: str  # holds | violated | incomplete
    residual_paise: int = 0
    ident_id: str = ""  # unique per identity; two identities may share the same total


def evaluate_identities(ext: StatementExtraction) -> list[IdentityResult]:
    out = []
    f = ext.fields
    for n, (total, comps) in enumerate(IDENTITIES[ext.statement]):
        iid = f"{total}#{n}"
        if total not in f or any(c not in f for c, _ in comps):
            out.append(IdentityResult(total, "incomplete", 0, iid))
            continue
        resid = f[total].value_paise - sum(s * f[c].value_paise for c, s in comps)
        out.append(IdentityResult(total, "holds" if resid == 0 else "violated", resid, iid))
    return out


def _digits_for(value_paise: int, unit: UnitScale | None) -> str:
    scale = (unit or UnitScale.RUPEES).value
    rupees = Decimal(abs(value_paise)) / 100 / scale
    if unit == UnitScale.LAKHS:
        return f"{rupees:.2f}".replace(".", "")
    return f"{rupees:.0f}"


def _edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def identity_membership(ext: StatementExtraction) -> dict[str, list[str]]:
    """code -> identity totals it participates in (as total or component)."""
    m: dict[str, list[str]] = {}
    for n, (total, comps) in enumerate(IDENTITIES[ext.statement]):
        for code in [total, *[c for c, _ in comps]]:
            m.setdefault(code, []).append(f"{total}#{n}")
    return m


def validate_statement(ext: StatementExtraction, max_edit: int = 2) -> list[IdentityResult]:
    """Annotate fields with validation outcomes; suggest single-cell repairs. Returns results."""
    f = ext.fields
    results = evaluate_identities(ext)
    for _ in range(3):
        violated = [r for r in results if r.status == "violated"]
        repaired_any = False
        for r in violated:
            comps = dict(IDENTITIES[ext.statement][int(r.ident_id.rsplit("#", 1)[1])][1])
            cells = [(r.total, 1), *[(c, s) for c, s in comps.items()]]
            # changing cell c by delta closes the identity when sign*delta == -residual-ish:
            # total - sum(s*comp) = resid  => total_new = total - resid ; comp_new = comp + resid/s
            candidates = []
            for code, _ in cells:
                # total = sum(s*comp) + resid, so closing the identity means total -= resid
                # or comp += resid * s (s is +1 or -1)
                if code == r.total:
                    new = f[code].value_paise - r.residual_paise
                else:
                    new = f[code].value_paise + r.residual_paise * comps[code]
                raw_digits = "".join(ch for ch in f[code].raw_text if ch.isdigit())
                d = _edit_distance(raw_digits, _digits_for(new, ext.unit))
                if d <= max_edit:
                    candidates.append((d, code, new))
            if len(candidates) >= 1:
                candidates.sort()
                if len(candidates) == 1 or candidates[0][0] < candidates[1][0]:
                    _, code, new = candidates[0]
                    if f[code].auto_corrected_from is None:
                        f[code].auto_corrected_from = f[code].value_paise
                        f[code].value_paise = new
                        f[code].validations.append(f"auto_corrected_by:{r.total}")
                        repaired_any = True
        results = evaluate_identities(ext)
        if not repaired_any:
            break
    member = identity_membership(ext)
    state = {res.ident_id: res for res in results}
    for code, ef in f.items():
        for tot in member.get(code, []):
            hit = state.get(tot)
            if hit is not None and hit.status == "violated":
                ef.validations.append(f"identity_violated:{tot}")
    if ext.duplicates:
        ext.doc_flags.append("duplicate_labels:" + ",".join(sorted(set(ext.duplicates))))
    return results


def identity_state(ext: StatementExtraction, results: list[IdentityResult], code: str) -> int:
    """0 = not covered / incomplete, 1 = in at least one holding identity and none violated,
    2 = in a violated identity."""
    member = identity_membership(ext)
    st = {r.ident_id: r.status for r in results}
    statuses = [st.get(t, "incomplete") for t in member.get(code, [])]
    if "violated" in statuses:
        return 2
    if "holds" in statuses:
        return 1
    return 0
