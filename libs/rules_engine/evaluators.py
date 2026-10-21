"""Evaluator logic per rule pack. Parameters come only from the pack (``p``); facts from callers.

Each evaluator returns ``(result, explanation, details)``. No regulatory number appears here.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from platform_core.errors import ValidationFailure

from rules_engine import evaluator


def _d(v: Any) -> date:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v))
    except ValueError as exc:
        raise ValidationFailure(f"not a valid ISO date: {v!r}", code="rule_fact_invalid") from exc


def _flag(f: dict[str, Any], name: str, default: bool | None = None) -> bool:
    """A boolean fact. Strings like "false" or "no" are rejected, not guessed at."""
    if name not in f:
        if default is None:
            raise KeyError(name)
        return default
    if not isinstance(f[name], bool):
        raise ValidationFailure(f"{name} must be a boolean", code="rule_fact_invalid")
    return bool(f[name])


# ------------------------------------------------------------------ SMA / IRAC ----------
_SEVERITY = {"STANDARD": 0, "SMA-0": 1, "SMA-1": 2, "SMA-2": 3, "NPA": 4}


def _count(f: dict[str, Any], name: str) -> int:
    """A required non-negative integer fact. Missing or malformed facts fail closed."""
    v = f[name]  # KeyError -> ValidationFailure at the registry boundary
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise ValidationFailure(f"{name} must be a non-negative integer", code="rule_fact_invalid")
    return v


@evaluator("rbi.sma")
def sma(p: dict[str, Any], f: dict[str, Any]) -> tuple[Any, str, dict[str, Any]]:
    """SMA/NPA bucket. Non-revolving: DPD bands. Revolving (CC/OD): days the balance has
    continuously exceeded limit/DP, *and* DPD on interest; the worse bucket wins.

    The facility type and DPD are mandatory: guessing "non-revolving, zero days" for a missing
    fact would silently classify a stressed cash credit as STANDARD."""
    npa_after = int(p["npa_after_days"])
    kind = str(f["type"]).strip().lower()
    if kind not in ("revolving", "non_revolving"):
        raise ValidationFailure(f"unknown facility type {f['type']!r}", code="rule_fact_invalid")
    dpd = _count(f, "dpd")
    candidates: list[tuple[str, str]] = []

    def band(table: list[dict[str, Any]], days: int, lo: str, hi: str) -> str:
        for row in table:
            if int(row[lo]) <= days <= int(row[hi]):
                return str(row["bucket"])
        return "STANDARD"

    if dpd > npa_after:
        candidates.append(("NPA", f"DPD {dpd} > {npa_after}"))
    else:
        candidates.append((band(p["non_revolving"], dpd, "dpd_min", "dpd_max"), f"DPD {dpd}"))
    if kind == "revolving":
        over = _count(f, "days_over_limit")
        if over > npa_after:
            candidates.append(("NPA", f"out of order {over} days > {npa_after}"))
        else:
            candidates.append(
                (band(p["revolving"], over, "days_min", "days_max"), f"over limit/DP {over} days")
            )
    bucket, why = max(candidates, key=lambda c: _SEVERITY[c[0]])
    return (
        bucket,
        f"{bucket} ({why})",
        {"dpd": dpd, "classification_time": p["classification_time"]},
    )


@evaluator("rbi.irac")
def irac(p: dict[str, Any], f: dict[str, Any]) -> tuple[Any, str, dict[str, Any]]:
    dpd = _count(f, "dpd")
    if dpd <= int(p["npa_after_days"]):
        cls = "STANDARD"
    else:
        months_npa = _count(f, "months_as_npa")
        cls = "SUBSTANDARD"
        for band in p["classes"]:
            if int(band["from_months"]) <= months_npa < int(band.get("to_months", 10**6)):
                cls = str(band["class"])
        if _flag(f, "loss_identified", default=False):
            cls = "LOSS"
    if not isinstance(f["secured"], bool):
        raise ValidationFailure("secured must be a boolean", code="rule_fact_invalid")
    secured = f["secured"]
    rate = p["provisioning_pct"][cls]["secured" if secured else "unsecured"]
    return cls, f"{cls}; provision {rate}%", {"provision_pct": str(rate)}


@evaluator("rbi.crilc")
def crilc(p: dict[str, Any], f: dict[str, Any]) -> tuple[Any, str, dict[str, Any]]:
    threshold_paise = int(Decimal(str(p["reporting_threshold_rupees"])) * 100)
    exp = _count(f, "aggregate_exposure_paise")
    reportable = exp >= threshold_paise
    return (
        reportable,
        f"aggregate exposure {'>=' if reportable else '<'} threshold",
        {"frequency": p["frequency"], "threshold_paise": threshold_paise},
    )


# ------------------------------------------------------------- registrations ------------
def _deadline_status(
    created: date, done: date | None, today: date, days: int, extra: int = 0
) -> tuple[str, date, int]:
    due = created + timedelta(days=days)
    if done is not None:
        if done <= due:
            return "ON_TIME", due, 0
        if extra and done <= due + timedelta(days=extra):
            return "LATE_IN_ADDITIONAL_WINDOW", due, (done - due).days
        return "LATE", due, (done - due).days
    if today <= due:
        return "PENDING", due, 0
    if extra and today <= due + timedelta(days=extra):
        return "OVERDUE_IN_ADDITIONAL_WINDOW", due, (today - due).days
    return "OVERDUE", due, (today - due).days


@evaluator("rbi.cersai")
def cersai(p: dict[str, Any], f: dict[str, Any]) -> tuple[Any, str, dict[str, Any]]:
    status, due, late = _deadline_status(
        _d(f["created_on"]),
        _d(f["registered_on"]) if f.get("registered_on") else None,
        _d(f["today"]),
        int(p["registration_days"]),
    )
    return status, f"CERSAI due {due}", {"due_date": due.isoformat(), "days_late": late}


@evaluator("statute.companies_act_chg1")
def chg1(p: dict[str, Any], f: dict[str, Any]) -> tuple[Any, str, dict[str, Any]]:
    status, due, late = _deadline_status(
        _d(f["created_on"]),
        _d(f["filed_on"]) if f.get("filed_on") else None,
        _d(f["today"]),
        int(p["filing_days"]),
        int(p["additional_window_days"]),
    )
    return status, f"CHG-1 due {due}", {"due_date": due.isoformat(), "days_late": late}


# ----------------------------------------------------------------- closure --------------
@evaluator("rbi.document_release")
def document_release(p: dict[str, Any], f: dict[str, Any]) -> tuple[Any, str, dict[str, Any]]:
    repaid = _d(f["repaid_on"])
    due = repaid + timedelta(days=int(p["release_within_days"]))
    end = _d(f["released_on"]) if f.get("released_on") else _d(f["today"])
    delay = max(0, (end - due).days)
    attributable = bool(f.get("delay_attributable_to_lender", True))
    per_day = Decimal(str(p["compensation_rupees_per_day"]))
    comp = per_day * delay if attributable else Decimal(0)
    escalate_at = repaid + timedelta(days=int(p["internal_escalation_day"]))
    return (
        {"due_date": due.isoformat(), "days_delayed": delay, "compensation_rupees": str(comp)},
        (f"release due {due}; delay {delay} days; compensation ₹{comp}"),
        {"escalate_on": escalate_at.isoformat()},
    )


# ----------------------------------------------------------------- fraud / RFA ----------
@evaluator("rbi.rfa")
def rfa(p: dict[str, Any], f: dict[str, Any]) -> tuple[Any, str, dict[str, Any]]:
    """Natural justice gate: fraud classification only after a show-cause notice and the
    minimum response window (or a received response)."""
    issued = f.get("show_cause_issued_on")
    if not issued:
        return False, "no show-cause notice recorded", {}
    window_end = _d(issued) + timedelta(days=int(p["show_cause_min_response_days"]))
    today = _d(f["today"])
    if _d(issued) > today:
        raise ValidationFailure("show-cause issued in the future", code="rule_fact_invalid")
    if f.get("response_received_on"):
        received = _d(f["response_received_on"])
        if received < _d(issued) or received > today:
            raise ValidationFailure(
                "response date outside the notice period", code="rule_fact_invalid"
            )
        return (
            True,
            "response received; committee may decide",
            {"window_end": window_end.isoformat()},
        )
    ok = _d(f["today"]) >= window_end
    return (
        ok,
        ("response window elapsed" if ok else f"response window open until {window_end}"),
        {"window_end": window_end.isoformat()},
    )


@evaluator("rbi.wilful_defaulter")
def wilful(p: dict[str, Any], f: dict[str, Any]) -> tuple[Any, str, dict[str, Any]]:
    outstanding = Decimal(str(f["outstanding_rupees"]))
    threshold = Decimal(str(p["threshold_rupees"]))
    if outstanding < threshold:
        return "NOT_ELIGIBLE", "outstanding below threshold", {}
    if not f.get("show_cause_issued_on"):
        return "SHOW_CAUSE_REQUIRED", "identification committee must issue show-cause", {}
    issued, today = _d(f["show_cause_issued_on"]), _d(f["today"])
    if issued > today:
        raise ValidationFailure("show-cause issued in the future", code="rule_fact_invalid")
    window_end = issued + timedelta(days=int(p["response_days"]))
    if f.get("response_received_on"):
        received = _d(f["response_received_on"])
        if received < issued or received > today:
            raise ValidationFailure(
                "response date outside the notice period", code="rule_fact_invalid"
            )
    elif today < window_end:
        return "AWAITING_RESPONSE", f"response window until {window_end}", {}
    if not _flag(f, "review_committee_confirmed", default=False):
        return "REVIEW_COMMITTEE_REQUIRED", "review committee must confirm", {}
    return "MAY_CLASSIFY", "all natural-justice steps complete", {}


# ---------------------------------------------------------- stressed assets -------------
@evaluator("rbi.prudential_framework")
def prudential(p: dict[str, Any], f: dict[str, Any]) -> tuple[Any, str, dict[str, Any]]:
    first_default = _d(f["first_default_on"])
    today = _d(f["today"])
    review_end = first_default + timedelta(days=int(p["review_period_days"]))
    deadline = review_end + timedelta(days=int(p["resolution_days_after_review"]))
    extra = Decimal(0)
    if not _flag(f, "plan_implemented", default=False):
        for step in p["additional_provisions"]:
            if today > review_end + timedelta(days=int(step["after_days_from_review_end"])):
                extra += Decimal(str(step["pct"]))
    return (
        {
            "review_period_end": review_end.isoformat(),
            "resolution_deadline": deadline.isoformat(),
            "additional_provision_pct": str(extra),
        },
        f"additional provision {extra}%",
        {},
    )


@evaluator("rbi.penal_charges")
def penal(p: dict[str, Any], f: dict[str, Any]) -> tuple[Any, str, dict[str, Any]]:
    reasons = []
    if f.get("kind") not in p["allowed_kinds"]:
        reasons.append(f"kind {f.get('kind')} not allowed")
    if f.get("capitalise") and not p["capitalisation_allowed"]:
        reasons.append("capitalisation of penal charges prohibited")
    if p["must_be_disclosed_in_agreement"] and not f.get("disclosed_in_agreement"):
        reasons.append("not disclosed in loan agreement")
    return (
        ("ALLOWED" if not reasons else "REJECTED"),
        "; ".join(reasons) or "compliant",
        {"reasons": reasons},
    )


@evaluator("rbi.large_exposures")
def large_exposures(p: dict[str, Any], f: dict[str, Any]) -> tuple[Any, str, dict[str, Any]]:
    tier1 = Decimal(str(f["tier1_capital_rupees"]))
    exposure = Decimal(str(f["exposure_rupees"]))
    kind = f.get("counterparty", "single")
    pct = Decimal(str(p["limits_pct_of_tier1"][kind]))
    if f.get("board_approved_extension") and kind == "single":
        pct += Decimal(str(p["board_extension_pct"]))
    cap = tier1 * pct / 100
    ok = exposure <= cap
    return (
        ok,
        f"{kind} exposure {'within' if ok else 'exceeds'} {pct}% of Tier 1",
        {"cap_rupees": str(cap)},
    )


@evaluator("rbi.ecl")
def ecl(p: dict[str, Any], f: dict[str, Any]) -> tuple[Any, str, dict[str, Any]]:
    dpd = _count(f, "dpd")
    if dpd > int(p["stage3_dpd_backstop"]) or f.get("credit_impaired"):
        return 3, "credit-impaired / DPD backstop", {}
    reasons = []
    if dpd > int(p["stage2_dpd_backstop"]):
        reasons.append(f"DPD {dpd} > {p['stage2_dpd_backstop']}")
    pd0, pd1 = f.get("pd_origination"), f.get("pd_current")
    if pd0 and pd1 and Decimal(str(pd1)) >= Decimal(str(pd0)) * Decimal(str(p["sicr_pd_multiple"])):
        reasons.append("PD multiple breach (SICR)")
    if f.get("ews_score") is not None and Decimal(str(f["ews_score"])) >= Decimal(
        str(p["sicr_ews_score"])
    ):
        reasons.append("EWS overlay")
    if f.get("watch_list") or f.get("restructured"):
        reasons.append("watch-list / restructured")
    return (2 if reasons else 1), "; ".join(reasons) or "no SICR", {"reasons": reasons}


# ---------------------------------------------------------------- policy ----------------
@evaluator("policy.credit_policy")
def credit_policy(p: dict[str, Any], f: dict[str, Any]) -> tuple[Any, str, dict[str, Any]]:
    violations: list[str] = []
    ft = f["facility_type"]
    cap = p["tenor_cap_months"].get(ft)
    if cap is None:
        violations.append(f"unknown facility type {ft!r}: no policy tenor cap")
    elif _count(f, "tenor_months") > int(cap):
        violations.append(f"tenor {f['tenor_months']}m > cap {cap}m for {ft}")
    floor = p["min_spread_bps_by_rating"].get(f.get("rating", ""), p["min_spread_bps_default"])
    if _count(f, "spread_bps") < int(floor):
        violations.append(
            f"spread {f['spread_bps']}bps < floor {floor}bps for rating {f.get('rating')}"
        )
    if ft in ("CC", "OD", "WCDL") and f.get("margin_pct") is None:
        violations.append("working-capital facility without a stated margin")
    if f.get("margin_pct") is not None:
        mn = Decimal(str(p["min_wc_margin_pct"]))
        if Decimal(str(f["margin_pct"])) * 100 < mn:
            violations.append(f"working-capital margin below {mn}%")
    return violations, "; ".join(violations) or "within policy", {}


@evaluator("policy.approval_matrix")
def approval_matrix(p: dict[str, Any], f: dict[str, Any]) -> tuple[Any, str, dict[str, Any]]:
    auth = f["authority"]
    power = p["delegated_power_rupees"].get(auth)
    if power is None:
        return False, f"unknown sanctioning authority {auth}", {}
    exposure = Decimal(str(f["total_exposure_rupees"]))
    ok = power == "unlimited" or exposure <= Decimal(str(power))
    return ok, f"{auth} power {power}; exposure {exposure}", {"power_rupees": str(power)}


@evaluator("policy.covenant_consequences")
def consequences(p: dict[str, Any], f: dict[str, Any]) -> tuple[Any, str, dict[str, Any]]:
    kind = str(f["consequence"]).split(":", 1)[0]
    mode = p["application"].get(kind, p["default_application"])
    return mode, f"{kind} applies {mode}", {}


@evaluator("policy.monitoring")
def monitoring(p: dict[str, Any], f: dict[str, Any]) -> tuple[Any, str, dict[str, Any]]:
    overdue = _count(f, "days_overdue")
    action = "none"
    for step in p["chase_sequence"]:
        if overdue >= int(step["after_days_overdue"]):
            action = str(step["action"])
    return action, f"{overdue} days overdue -> {action}", {}


@evaluator("ews.indicators")
def ews_indicators(p: dict[str, Any], f: dict[str, Any]) -> tuple[Any, str, dict[str, Any]]:
    metrics: dict[str, Any] = f["metrics"]
    hits: list[str] = []
    missing: list[str] = []
    for ind in p["indicators"]:
        v = metrics.get(ind["metric"])
        if v is None:
            missing.append(str(ind["id"]))  # not evaluated: reported, never silently "clear"
            continue
        v, t = Decimal(str(v)), Decimal(str(ind["threshold"]))
        op = ind["operator"]
        fired = (
            (v >= t)
            if op == ">="
            else (v > t)
            if op == ">"
            else (v <= t)
            if op == "<="
            else (v < t)
        )
        if fired:
            hits.append(str(ind["id"]))
    return (
        hits,
        f"{len(hits)} indicator(s) fired; {len(missing)} not evaluable",
        {"not_evaluated": missing},
    )


@evaluator("policy.covenant_library")
def covenant_library(p: dict[str, Any], f: dict[str, Any]) -> tuple[Any, str, dict[str, Any]]:
    c = p["covenants"][f["covenant"]]
    return c["operator"], c["name"], {"formula": c["formula"], "definitions": p["definitions"]}


# ------------------------------------------------------------ alerting & reviews ---------
@evaluator("ews.alerting")
def ews_alerting(p: dict[str, Any], f: dict[str, Any]) -> tuple[Any, str, dict[str, Any]]:
    """Fuse fired indicators and the model score into one score and level, with hysteresis so an
    alert does not flap, and a data-gap flag when too many indicators could not be evaluated."""
    points = p["severity_points"]
    hits = f["hit_severities"]
    if not isinstance(hits, list) or any(s not in points for s in hits):
        raise ValidationFailure(
            "hit_severities must name known severities", code="rule_fact_invalid"
        )
    rule_score = min(100, sum(int(points[s]) for s in hits))
    ml = f.get("ml_score")
    if ml is not None:
        try:
            ok = not isinstance(ml, bool) and 0 <= Decimal(str(ml)) <= 100
        except ArithmeticError:
            ok = False
        if not ok:
            raise ValidationFailure("ml_score must be between 0 and 100", code="rule_fact_invalid")
    score = max(rule_score, int(Decimal(str(ml))) if ml is not None else 0)
    levels = p["level_thresholds"]  # ordered low -> high
    order = ["none"] + [str(r["level"]) for r in levels]
    level = "none"
    for row in levels:
        if score >= int(row["min_score"]):
            level = str(row["level"])
    prev = f.get("previous_level")
    if prev is not None and prev != "none":
        if prev not in order:
            raise ValidationFailure("unknown previous_level", code="rule_fact_invalid")
        clear = int(next(r["clear_below"] for r in levels if r["level"] == prev))
        if order.index(level) < order.index(prev) and score >= clear:
            level = prev  # stay at the higher level until the score clears the lower bound
    total = _count(f, "total_indicators")
    unev = _count(f, "unevaluated")
    gap = total > 0 and Decimal(unev) / Decimal(total) > Decimal(str(p["max_unevaluated_share"]))
    return (
        {"score": score, "level": level, "data_gap": gap},
        f"score {score} level {level}" + ("; data gap" if gap else ""),
        {"rule_score": rule_score, "ml_score": ml},
    )


def _add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y, mo = d.year + m // 12, m % 12 + 1
    import calendar

    return date(y, mo, min(d.day, calendar.monthrange(y, mo)[1]))


@evaluator("policy.review_calendar")
def review_calendar(p: dict[str, Any], f: dict[str, Any]) -> tuple[Any, str, dict[str, Any]]:
    rating = str(f["rating"])
    months = int(p["review_interval_months"].get(rating, p["default_interval_months"]))
    if _flag(f, "watch_list", default=False):
        months = min(months, int(p["watch_list_interval_months"]))
    last = _d(f["last_review_on"])
    today = _d(f["today"])
    if last > today:
        raise ValidationFailure("last review is in the future", code="rule_fact_invalid")
    due = _add_months(last, months)
    late = max(0, (today - due).days)
    return (
        {"next_review_due": due.isoformat(), "overdue": late > 0, "days_overdue": late},
        f"review every {months} months; due {due}",
        {"interval_months": months},
    )
