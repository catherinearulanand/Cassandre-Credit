"""The demonstration must not overstate itself: every step carries an honest connection label."""

from __future__ import annotations

from pathlib import Path

import pytest

from sim.demo_story import main

ALLOWED_PREFIXES = ("REAL", "SIMULATED", "NOT BUILT")


@pytest.fixture(scope="module")
def report(tmp_path_factory: pytest.TempPathFactory) -> dict:  # type: ignore[type-arg]
    import json

    out = tmp_path_factory.mktemp("demo") / "story.json"
    assert main(["--engine", "none", "--pool", "120", "--out", str(out)]) == 0
    return json.loads(Path(out).read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def test_four_archetypes_are_replayed(report) -> None:  # type: ignore[no-untyped-def]
    assert sorted(j["archetype"] for j in report["journeys"]) == [
        "breach",
        "default",
        "negotiated",
        "steady",
    ]


def test_every_step_is_labelled_and_dated_in_order(report) -> None:  # type: ignore[no-untyped-def]
    for j in report["journeys"]:
        dates = [s["when"] for s in j["steps"]]
        assert dates == sorted(dates)
        for s in j["steps"]:
            assert s["mode"].startswith(ALLOWED_PREFIXES), s["mode"]
            assert s["text"] and s["component"]


def test_people_and_finacle_are_never_presented_as_real(report) -> None:  # type: ignore[no-untyped-def]
    for j in report["journeys"]:
        for s in j["steps"]:
            if s["component"] in (
                "Credit monitoring officer",
                "Recovery head",
                "RFA committee",
                "Borrower portal",
            ):
                assert s["mode"].startswith("SIMULATED")
            if s["component"] == "Finacle adapter":
                assert "SIMULATED Finacle" in s["mode"]
            if s["component"] == "LLM clause reader":
                assert s["mode"] == "NOT BUILT"


def test_the_controls_show_up_in_the_story(report) -> None:  # type: ignore[no-untyped-def]
    texts = {j["archetype"]: " ".join(s["text"] for s in j["steps"]) for j in report["journeys"]}
    assert "maker_equals_checker" in texts["breach"] and "REJECT" in texts["breach"]
    assert "rfa_not_ready" in texts["default"] and "NPA" in texts["default"]
    assert "Zero-dues proof" in texts["steady"] and "days delayed 0" in texts["steady"]
    assert "grammar could not read it" in texts["negotiated"]
