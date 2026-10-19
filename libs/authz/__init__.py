"""RBAC + ABAC authorisation, maker-checker enforcement and signed approval records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from platform_core.canonical import content_hash
from platform_core.clock import Clock
from platform_core.errors import AuthorizationError
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
        {Permission.READ_PORTFOLIO, Permission.READ_OBLIGOR, Permission.RFA_CLASSIFY}
    ),
    Role.MODEL_VALIDATOR: frozenset({Permission.MODEL_PROMOTE}),
    Role.AUDITOR: frozenset(
        {Permission.AUDIT_REPLAY, Permission.READ_OBLIGOR, Permission.READ_PORTFOLIO}
    ),
    Role.BORROWER: frozenset({Permission.UPLOAD_DOCUMENT}),
    Role.AGENT: frozenset({Permission.CTS_DRAFT, Permission.READ_OBLIGOR}),
    Role.SERVICE: frozenset(),
}

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
    checker_id: str
    checker_roles: tuple[str, ...]
    reason: str
    at: datetime
    signature: Signature


class MakerChecker:
    """Creates signed approval records. The checker must be a different, human principal."""

    def __init__(self, clock: Clock, signer: Signer) -> None:
        self._clock = clock
        self._signer = signer

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
    ) -> ApprovalRecord:
        if checker.kind != "human":
            raise AuthorizationError("checker must be a human", code="checker_not_human")
        if checker.id == maker_id:
            raise AuthorizationError("maker and checker must differ", code="maker_equals_checker")
        if not reason.strip():
            raise AuthorizationError("approval requires a written reason", code="reason_required")
        authorize(checker, required, scope)
        h = content_hash({"subject": subject, "payload": subject_payload, "action": action})
        sig = self._signer.sign_hash(h)
        return ApprovalRecord(
            approval_id=new_id("APR"),
            subject=subject,
            subject_hash=h,
            action=action,
            maker_id=maker_id,
            checker_id=checker.id,
            checker_roles=tuple(sorted(r.value for r in checker.roles)),
            reason=reason,
            at=self._clock.now(),
            signature=sig,
        )

    def verify(self, record: ApprovalRecord, subject_payload: object) -> None:
        h = content_hash(
            {"subject": record.subject, "payload": subject_payload, "action": record.action}
        )
        self._signer.verify(record.signature, h)
