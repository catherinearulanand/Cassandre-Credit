"""Small-book simulation as a regression test: engine vs independent calculators must agree
exactly, the gate must follow covenant policy, and the models must beat trivial baselines."""

from __future__ import annotations

import pytest

from sim.book import build_book
from sim.run_simulation import (
    stage_closure,
    stage_covenants,
    stage_dp,
    stage_gate,
    stage_models,
    stage_publish,
    stage_sanction,
    stage_sma,
)


@pytest.fixture(scope="module")
def world():  # type: ignore[no-untyped-def]
    book = build_book(250, seed=77)
    cov_report, cov = stage_covenants(book)
    return book, cov_report, cov


def test_covenant_engine_matches_independent_calculation(world) -> None:  # type: ignore[no-untyped-def]
    _, rep, _ = world
    assert rep["tests_run"] > 2000
    assert rep["value_matches_independent_calculation"]["value"] == 1.0
    assert rep["result_matches_independent_calculation"]["value"] == 1.0
    assert rep["lineage_coverage"]["value"] == 1.0
    assert rep["latency_ms"]["p99"] < 60_000, (
        "a covenant test must finish well inside the 1-minute SLA"
    )


def test_gate_follows_covenant_policy_and_is_fast(world) -> None:  # type: ignore[no-untyped-def]
    book, _, cov = world
    rep = stage_gate(book, cov)
    assert rep["gate_matches_covenant_policy"]["value"] == 1.0
    assert rep["latency_ms"]["p99"] < 300


def test_sma_dp_closure_and_sanction(world) -> None:  # type: ignore[no-untyped-def]
    book, _, _ = world
    assert stage_sma(book)["match_independent_oracle"]["value"] == 1.0
    dp = stage_dp(book, 5)
    assert dp["match_hand_oracle"]["value"] == 1.0 and dp["invariant_violations"] == 0
    cl = stage_closure()
    assert cl["D+25"]["days_delayed"] == 0 and cl["D+31"]["days_delayed"] == 1
    assert (
        cl["D+31"]["compensation_rupees"] == "5000" and cl["D+40"]["compensation_rupees"] == "50000"
    )
    s = stage_sanction(book)
    assert s["template_letters"]["cts_exactly_equal_to_truth"]["value"] == 1.0
    assert s["citation_coverage"]["value"] == 1.0
    # the grammar cannot read negotiated agreements: this is the measured case for an LLM
    assert s["negotiated_agreements"]["needs_llm_or_human"]["value"] > 0.9
    pub = stage_publish(book)
    assert pub["published"] + pub["blocked_by_validation"] == len(book)


def test_models_beat_trivial_baselines_out_of_time(world) -> None:  # type: ignore[no-untyped-def]
    book, _, cov = world
    m = stage_models(book, cov)
    e = m["ews"]
    assert (
        e["metrics"]["auc"] > 0.70 and e["metrics"]["auc"] > e["baselines"]["persistence_auc"]
    )
    assert e["metrics"]["ece_calibrated"] < 0.15
    assert all(len(r["reasons"]) == 5 for r in e["reasons_sample"])
    assert m["breach_forecast"]["metrics"]["auc"] > 0.8
