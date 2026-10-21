"""One borrower's stress, end to end: signals, agent proposal, human decision, gate, classification.

The point is the control flow: an agent's proposal changes nothing until a human approves it, only
then does the gate stop new money, repayments stay open, and the classification is rule-driven.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from authz import MakerChecker, Principal, ResourceScope, Role
from case_service.domain import CaseService
from domain.entities import CaseType
from domain.limit_tree import LimitState, LimitTree
from drawdown_gate.domain import (
    CovenantStatus,
    DrawdownGate,
    DrawdownRequest,
    GateContext,
    RepaymentNotGated,
)
from ews_service.domain import EwsService
from platform_core.clock import FixedClock
from platform_core.errors import AuthorizationError
from rfa_service.domain import RfaService
from rules_engine import default_registry
from security.crypto import LocalDevKeyProvider, Signer
from sma_service.domain import AccountSnapshot, SmaService

from agents.ews_analyst import agent as ews_analyst
from agents.runtime import AgentRun, AgentSpec, ToolSpec

ROOT = Path(__file__).resolve().parents[2]
SCOPE = ResourceScope("WEST", "MIDCORP", "OBL-1")
CLK = FixedClock(datetime(2026, 10, 21, 9, 0, tzinfo=UTC))


class Ctx:
    """The gate's view is BUILT FROM the services: classification, RFA flag, approved precautions.
    Deleting or breaking any of them changes the gate's answer."""

    def __init__(self, sma: SmaService, rfa: RfaService, cases: CaseService) -> None:
        self.sma, self.rfa, self.cases = sma, rfa, cases

    def load(self, facility_id: str) -> GateContext:
        tree = LimitTree()
        tree.add(LimitState("OBL", None, 1_000))
        tree.add(LimitState("CC", "OBL", 600, utilised=100, dp=500, facility_id="F"))
        stops = tuple(
            p.payload["consequence"]
            for c in self.cases.cases.values()
            if c.obligor_id == "OBL-1"
            for p in c.proposals
            if p.kind == "consequence" and p.status == "approved"
        )
        covs = [CovenantStatus("EWS-PRECAUTION", "BREACHED", stops)] if stops else []
        return GateContext(
            cts_valid_until=date(2027, 6, 1),
            facility_limit_node="CC",
            open_cps=[],
            overdue_stopping_cs=[],
            tree=tree,
            dp_paise=500,
            utilised_paise=100,
            covenants=covs,
            rfa_or_fraud_flag=self.rfa.is_flagged("OBL-1"),
            sma_bucket=self.sma.borrower_view("OBL-1")["worst_bucket"],
            amendment_in_flight=False,
            approved_payees={"SUP-1"},
            facility_id="F",
            as_of=CLK.today(),
        )


def test_stress_to_gate_requires_a_human_and_never_blocks_repayment() -> None:
    reg = default_registry()
    reg._today = CLK.today
    cases = CaseService(CLK, MakerChecker(CLK, Signer(LocalDevKeyProvider())))
    ews, sma, rfa = EwsService(CLK, reg), SmaService(CLK, reg), RfaService(CLK, reg, cases)
    ctx = Ctx(sma, rfa, cases)
    gate = DrawdownGate(ctx, CLK.today)
    req = DrawdownRequest("DRW-1", "F", 100, payee_id="SUP-1")
    assert gate.decide(req).decision == "APPROVE"

    # 1. Stress shows up in conduct data.
    state = ews.update("OBL-1", {"cheque_returns_90d": 5, "overdrawn_days_90d": 25})
    assert state.level == "critical"

    # 2. The agent analyses and PROPOSES a precaution. Nothing changes yet.
    case = cases.open_case(CaseType.EWS, "OBL-1", SCOPE, "critical EWS", sla_days=2)
    officer = Principal(
        id="mo1",
        kind="human",
        roles=frozenset({Role.MONITORING_OFFICER}),
        regions=frozenset({"WEST"}),
    )
    tools = {
        "ews.read_state": ToolSpec(
            "ews.read_state",
            lambda obligor_id: {
                "score": ews.state[obligor_id].score,
                "level": ews.state[obligor_id].level,
                "fired": list(ews.state[obligor_id].fired),
                "not_evaluated": list(ews.state[obligor_id].not_evaluated),
                "data_gap": ews.state[obligor_id].data_gap,
            },
            "read",
        ),
        "case.propose": ToolSpec("case.propose", cases.propose, "propose"),
    }
    run = AgentRun(
        AgentSpec.load(ROOT / "agents/ews_analyst/policy.yaml"), officer, lambda n, k: SCOPE, tools
    )
    res = ews_analyst.run(run, obligor_id="OBL-1", case_id=case.case_id)
    assert len(res.proposals) == 1
    assert gate.decide(req).decision == "APPROVE", "a pending proposal must not stop anything"

    # 3. A named human decides; only then is the precaution real and visible to the gate.
    prop = case.proposals[0]
    with pytest.raises(AuthorizationError):  # the human the agent worked for cannot also approve
        cases.decide(case.case_id, prop.proposal_id, checker=officer, approve=True, reason="self")
    second = Principal(
        id="mo2",
        kind="human",
        roles=frozenset({Role.MONITORING_OFFICER}),
        regions=frozenset({"WEST"}),
    )
    cases.decide(
        case.case_id, prop.proposal_id, checker=second, approve=True, reason="critical EWS"
    )
    assert prop.status == "approved" and prop.approval is not None
    d = gate.decide(req)
    assert d.decision == "REJECT" and any("covenant" in r.lower() for r in d.reasons)

    # 4. Repayments are never gated.
    with pytest.raises(RepaymentNotGated):
        gate.decide(DrawdownRequest("RPY-1", "F", 100, kind="repayment"))

    # 5. Classification is rule-driven and now what the gate reads.
    sma.classify(
        AccountSnapshot(
            "ACC-1",
            "OBL-1",
            False,
            dpd=100,
            days_over_limit=0,
            outstanding_paise=10**8,
            secured=True,
            npa_since=CLK.today(),
        )
    )
    cases.cases[case.case_id].proposals[0].status = "rejected"  # isolate: only the NPA matters now
    d = gate.decide(req)
    assert d.decision == "REJECT" and any(
        "npa" in r.lower() or "sma" in r.lower() for r in d.reasons
    )

    # 6. An open RFA file blocks the gate by itself; closing it (after a decision) lifts that block.
    sma.classify(
        AccountSnapshot(
            "ACC-1",
            "OBL-1",
            False,
            dpd=0,
            days_over_limit=0,
            outstanding_paise=10**8,
            secured=True,
            arrears_cleared=True,
        )
    )
    assert gate.decide(req).decision == "APPROVE"
    f = rfa.trigger("OBL-1", SCOPE, indicators=["EWS02_overdrawn_days"])
    d = gate.decide(req)
    assert d.decision == "REJECT" and any(
        "fraud" in r.lower() or "rfa" in r.lower() for r in d.reasons
    )
    rfa.close_file(f.rfa_id, reason="no fraud found", by="committee-1")
    assert gate.decide(req).decision == "APPROVE"

    # 7. The audit trail names the agent, the human it acted for, and what it did.
    assert run.calls[0]["tool"] == "ews.read_state" and prop.proposed_by == "ews_analyst"
    assert prop.on_behalf_of == "mo1"
