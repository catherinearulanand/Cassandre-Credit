"""RBAC + ABAC authorisation, maker-checker enforcement and signed approval records."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from platform_core.canonical import content_hash
from platform_core.clock import Clock
from platform_core.errors import AuthorizationError, IntegrityError
from platform_core.ids import new_id
from security.crypto import Signature, Signer


class Role(StrEnum):
    CAD_MAKER = "cad_maker"
    CAD_CHECKER = "cad_checker"
    RM = "relationship_manager"
    MONITORING_OFFICER = "credit_monitoring_officer"
    SANCTIONING_AUTHORITY = "sanctioning_authority"
    LEGAL_OFFICER = "legal_officer"
    RECOVERY_HEAD = "recovery_head"
    CRO = "chief_risk_officer"
    MODEL_VALIDATOR = "model_validator"
    AUDITOR = "auditor"
    BORROWER = "borrower_user"
    AGENT = "agent"
    SERVICE = "service"


class Permission(StrEnum):
    CTS_DRAFT = "cts:draft"
    CTS_PUBLISH = "cts:publish"
    CTS_AMEND = "cts:amend"
    CASE_DECIDE = "case:decide"
    WAIVER_APPROVE = "covenant:waive"
    CONSEQUENCE_APPROVE = "limit:apply_consequence"
    LIMIT_CHANGE = "limit:change"
    DOC_DEVIATION_CLEAR = "documentation:clear_deviation"
    RFA_CLASSIFY = "rfa:classify_fraud"
    OTS_APPROVE = "recovery:approve_ots"
    LETTER_SEND = "borrower:send_letter"
    READ_PORTFOLIO = "portfolio:read"
    READ_OBLIGOR = "obligor:read"
    UPLOAD_DOCUMENT = "document:upload"
    MODEL_PROMOTE = "model:promote"
    AUDIT_REPLAY = "audit:replay"
    RULES_OVERRIDE = "rules:override_unverified"
    LLM_CONTROL = "llm:kill_switch"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.CAD_MAKER: frozenset({Permission.CTS_DRAFT, Permission.READ_OBLIGOR}),
    Role.CAD_CHECKER: frozenset({Permission.CTS_PUBLISH, Permission.READ_OBLIGOR}),
    Role.RM: frozenset({Permission.READ_OBLIGOR, Permission.READ_PORTFOLIO}),
    Role.MONITORING_OFFICER: frozenset(
        {
            Permission.CASE_DECIDE,
            Permission.LETTER_SEND,
            Permission.READ_OBLIGOR,
            Permission.CONSEQUENCE_APPROVE,
        }
    ),
    Role.SANCTIONING_AUTHORITY: frozenset(
        {
            Permission.CTS_AMEND,
            Permission.WAIVER_APPROVE,
            Permission.CASE_DECIDE,
            Permission.LIMIT_CHANGE,
            Permission.CONSEQUENCE_APPROVE,
            Permission.READ_OBLIGOR,
        }
    ),
    Role.LEGAL_OFFICER: frozenset({Permission.DOC_DEVIATION_CLEAR, Permission.READ_OBLIGOR}),
    Role.RECOVERY_HEAD: frozenset(
        {Permission.OTS_APPROVE, Permission.CASE_DECIDE, Permission.READ_OBLIGOR}
    ),
    Role.CRO: frozenset(
        {
            Permission.READ_PORTFOLIO,
            Permission.READ_OBLIGOR,
            Permission.RFA_CLASSIFY,
            Permission.LLM_CONTROL,
        }
    ),
    Role.MODEL_VALIDATOR: frozenset({Permission.MODEL_PROMOTE}),
    Role.AUDITOR: frozenset(
        {Permission.AUDIT_REPLAY, Permission.READ_OBLIGOR, Permission.READ_PORTFOLIO}
    ),
    Role.BORROWER: frozenset({Permission.UPLOAD_DOCUMENT}),
    Role.AGENT: frozenset({Permission.CTS_DRAFT, Permission.READ_OBLIGOR}),
    Role.SERVICE: frozenset(),
}

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9:_.@\-]{0,127}")
MONEY_PERMISSIONS = frozenset({Permission.LIMIT_CHANGE, Permission.OTS_APPROVE})
HUMAN_ROLES = frozenset(r for r in Role if r not in (Role.AGENT, Role.SERVICE))


@dataclass(frozen=True)
class Principal:
    id: str
    kind: str  # human | agent | service
    roles: frozenset[Role]
    regions: frozenset[str] = field(default_factory=lambda: frozenset({"*"}))
    portfolios: frozenset[str] = field(default_factory=lambda: frozenset({"*"}))
    obligor_ids: frozenset[str] | None = None  # borrower users are pinned to their own obligor
    on_behalf_of: str | None = None  # agents act for a named human
    delegated_power_paise: int = 0  # sanctioning authority's delegated financial power
    # an agent's rights are the intersection of its own role and the rights of the human it acts for
    delegator_permissions: frozenset[Permission] | None = None

    def __post_init__(self) -> None:
        if not _ID_RE.fullmatch(self.id):
            raise AuthorizationError("principal ids must be plain ASCII", code="bad_id")
        if self.kind == "agent" and not _ID_RE.fullmatch(self.on_behalf_of or ""):
            raise AuthorizationError("agent principal needs a human", code="agent_unbound")
        if self.kind == "agent" and self.delegator_permissions is None:
            raise AuthorizationError("build agents with Principal.for_agent", code="agent_unbound")
        if self.kind == "service" and not self.roles <= {Role.SERVICE}:
            raise AuthorizationError("a service may hold only the service role", code="bad_role")
        if self.kind not in ("human", "agent", "service"):
            raise AuthorizationError(f"unknown principal kind {self.kind!r}", code="bad_kind")
        if self.kind == "agent" and not (self.on_behalf_of and self.on_behalf_of.strip()):
            raise AuthorizationError("agent principal needs a human", code="agent_unbound")
        if self.kind == "agent" and self.roles != frozenset({Role.AGENT}):
            raise AuthorizationError("an agent may hold only the agent role", code="bad_role")
        if self.kind != "agent" and Role.AGENT in self.roles:
            raise AuthorizationError(
                "only agent principals may hold the agent role", code="bad_role"
            )

    @classmethod
    def for_agent(cls, agent_id: str, human: Principal) -> Principal:
        """An agent never exceeds the human it acts for: scope is inherited, not chosen."""
        if human.kind != "human":
            raise AuthorizationError("an agent must act for a human", code="agent_unbound")
        return cls(
            id=agent_id,
            kind="agent",
            roles=frozenset({Role.AGENT}),
            regions=human.regions,
            portfolios=human.portfolios,
            obligor_ids=human.obligor_ids,
            on_behalf_of=human.id,
            delegator_permissions=human.permissions(),
        )

    def permissions(self) -> frozenset[Permission]:
        perms: set[Permission] = set()
        for r in self.roles:
            perms |= ROLE_PERMISSIONS[r]
        return frozenset(perms)


@dataclass(frozen=True)
class ResourceScope:
    region: str
    portfolio: str
    obligor_id: str


def authorize(principal: Principal, perm: Permission, scope: ResourceScope | None = None) -> None:
    """Raise AuthorizationError unless ``principal`` may perform ``perm`` on ``scope``.

    Agents can never hold decision permissions regardless of role mapping.
    """
    if perm not in principal.permissions():
        raise AuthorizationError(f"{principal.id} lacks {perm}", code="missing_permission")
    if principal.kind == "agent":
        if perm not in (Permission.CTS_DRAFT, Permission.READ_OBLIGOR, Permission.READ_PORTFOLIO):
            raise AuthorizationError(
                "agents cannot hold decision permissions", code="agent_forbidden"
            )
        if not principal.on_behalf_of:
            raise AuthorizationError("agent call without a human principal", code="agent_unbound")
        if perm not in (principal.delegator_permissions or frozenset()):
            raise AuthorizationError(
                "the human behind this agent lacks that right", code="agent_cap"
            )
    if scope is not None:
        if "*" not in principal.regions and scope.region not in principal.regions:
            raise AuthorizationError("outside region scope", code="abac_region")
        if "*" not in principal.portfolios and scope.portfolio not in principal.portfolios:
            raise AuthorizationError("outside portfolio scope", code="abac_portfolio")
        if principal.obligor_ids is not None and scope.obligor_id not in principal.obligor_ids:
            raise AuthorizationError("outside obligor scope", code="abac_obligor")


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str
    subject: str  # e.g. "CTS-...:v2" or "CASE-...:consequence:stop_drawdown"
    subject_hash: str  # content hash of exactly what was approved
    action: str
    maker_id: str
    maker_on_behalf_of: str | None
    checker_id: str
    checker_roles: tuple[str, ...]
    reason: str
    at: datetime
    amount_paise: int | None
    required: str
    signature: Signature

    def record_body(self) -> dict[str, object]:
        """Everything except the signature. The signature covers all of it."""
        return {
            "approval_id": self.approval_id,
            "subject": self.subject,
            "subject_hash": self.subject_hash,
            "action": self.action,
            "maker_id": self.maker_id,
            "maker_on_behalf_of": self.maker_on_behalf_of,
            "checker_id": self.checker_id,
            "checker_roles": list(self.checker_roles),
            "reason": self.reason,
            "at": self.at.isoformat(),
            "amount_paise": self.amount_paise,
            "required": self.required,
        }


def _norm(identity: str) -> str:
    """Canonical form of an identity for comparison: NFKC, no format/control characters, folded."""
    t = unicodedata.normalize("NFKC", identity)
    t = "".join(ch for ch in t if unicodedata.category(ch) not in ("Cf", "Cc"))
    return " ".join(t.split()).casefold()


class MakerChecker:
    """Creates signed approval records. The checker must be a different, human principal."""

    def __init__(self, clock: Clock, signer: Signer) -> None:
        self._clock = clock
        self._signer = signer
        self.records: dict[str, ApprovalRecord] = {}

    def approve(
        self,
        *,
        subject: str,
        subject_payload: object,
        action: str,
        maker_id: str,
        checker: Principal,
        required: Permission,
        reason: str,
        scope: ResourceScope | None = None,
        maker_on_behalf_of: str | None = None,
        amount_paise: int | None = None,
    ) -> ApprovalRecord:
        if checker.kind != "human":
            raise AuthorizationError("checker must be a human", code="checker_not_human")
        for name in (maker_id, maker_on_behalf_of):
            if name is not None and not _ID_RE.fullmatch(name):
                raise AuthorizationError("maker identity must be plain ASCII", code="bad_id")
        if (
            _norm(maker_id).startswith(("agent:", "agent-", "agent_", "svc:", "svc-"))
            and not maker_on_behalf_of
        ):
            raise AuthorizationError(
                "an agent or service maker must name the human it acts for", code="maker_unbound"
            )
        # An agent maker is the human it acts for: that human cannot also be the checker.
        makers = {_norm(maker_id)} | ({_norm(maker_on_behalf_of)} if maker_on_behalf_of else set())
        if _norm(checker.id) in makers:
            raise AuthorizationError("maker and checker must differ", code="maker_equals_checker")
        if not reason.strip():
            raise AuthorizationError("approval requires a written reason", code="reason_required")
        authorize(checker, required, scope)
        if required in MONEY_PERMISSIONS and amount_paise is None:
            raise AuthorizationError("this approval must state the amount", code="amount_required")
        if amount_paise is not None and (isinstance(amount_paise, bool) or amount_paise < 0):
            raise AuthorizationError("amount must be a non-negative integer", code="bad_amount")
        if amount_paise is not None and amount_paise > checker.delegated_power_paise:
            raise AuthorizationError(
                "amount exceeds the checker's delegated financial power", code="delegated_power"
            )
        h = content_hash({"subject": subject, "payload": subject_payload, "action": action})
        draft = ApprovalRecord(
            approval_id=new_id("APR"),
            subject=subject,
            subject_hash=h,
            action=action,
            maker_id=maker_id,
            maker_on_behalf_of=maker_on_behalf_of,
            checker_id=checker.id,
            checker_roles=tuple(sorted(r.value for r in checker.roles)),
            reason=reason,
            at=self._clock.now(),
            amount_paise=amount_paise,
            required=required.value,
            signature=Signature("", "", ""),
        )
        sig = self._signer.sign_hash(content_hash(draft.record_body()))
        record = ApprovalRecord(**{**draft.__dict__, "signature": sig})
        self.records[record.approval_id] = record
        return record

    def verify(
        self, record: ApprovalRecord, subject_payload: object, *, required: Permission | None = None
    ) -> None:
        """Verify the whole record (every field is signed) and its binding to the payload."""
        stored = self.records.get(record.approval_id)
        if stored is not None and stored != record:
            raise IntegrityError("approval record differs from the issued record")
        self._signer.verify(record.signature, content_hash(record.record_body()))
        h = content_hash(
            {"subject": record.subject, "payload": subject_payload, "action": record.action}
        )
        if h != record.subject_hash:
            raise IntegrityError("approval is not bound to this payload")
        if required is not None and record.required != required.value:
            raise IntegrityError("approval was granted for a different permission")

    def verify_id(
        self, approval_id: str, subject_payload: object, *, required: Permission | None = None
    ) -> ApprovalRecord:
        record = self.records.get(approval_id)
        if record is None:
            raise IntegrityError(f"unknown approval {approval_id}")
        self.verify(record, subject_payload, required=required)
        return record
