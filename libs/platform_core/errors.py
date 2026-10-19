"""Typed error hierarchy. API layers map these to RFC 9457 problem details."""

from __future__ import annotations


class DomainError(Exception):
    """Base for all business-rule failures. ``code`` is stable and machine-readable."""

    code: str = "domain_error"
    http_status: int = 422

    def __init__(self, message: str, *, code: str | None = None, **context: object) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.context = context

    def to_problem(self) -> dict[str, object]:
        return {
            "type": f"https://credit-platform.internal/problems/{self.code}",
            "title": self.code.replace("_", " ").capitalize(),
            "status": self.http_status,
            "detail": str(self),
            "code": self.code,
        }


class ValidationFailure(DomainError):
    code = "validation_failed"


class NotFoundError(DomainError):
    code = "not_found"
    http_status = 404


class ConflictError(DomainError):
    code = "conflict"
    http_status = 409


class AuthorizationError(DomainError):
    code = "forbidden"
    http_status = 403


class IntegrityError(DomainError):
    """Tamper evidence: a hash chain, signature or content hash failed to verify."""

    code = "integrity_violation"
    http_status = 409
