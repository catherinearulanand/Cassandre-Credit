"""Covenant test calendar shared by the covenant engine and the monitoring calendar."""

from __future__ import annotations

from datetime import date, timedelta

from domain.cts import CovenantDef


def due_test_dates(cov: CovenantDef, until: date) -> list[date]:
    """Quarter/half/year ends from first_test_date up to ``until`` (Indian FY calendar)."""
    if cov.first_test_date is None or cov.test_frequency == "event":
        return []
    step = {"monthly": 1, "quarterly": 3, "half_yearly": 6, "annual": 12}[cov.test_frequency]
    out, d = [], cov.first_test_date
    while d <= until:
        out.append(d)
        m = d.month + step
        y = d.year + (m - 1) // 12
        m = (m - 1) % 12 + 1
        # month end
        nxt = date(y + (m // 12), m % 12 + 1, 1) - timedelta(days=1)
        d = nxt
    return out
