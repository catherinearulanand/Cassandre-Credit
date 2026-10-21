"""Ports workflow services use to reach other services without importing them.

Services never import each other (architecture test). Where one needs another, it depends on a
Protocol defined here and the application wires the real implementation.
"""

from __future__ import annotations

from typing import Any, Protocol

from authz import Permission, ResourceScope

from domain.entities import CaseType


class CaseRef(Protocol):
    @property
    def case_id(self) -> str: ...


class CasePort(Protocol):
    """Open cases and *propose* decisions. Deciding is a separate, human, maker-checker act."""

    def open_case(
        self,
        case_type: CaseType,
        obligor_id: str,
        scope: ResourceScope,
        summary: str,
        *,
        sla_days: int,
        evidence: list[str] | None = None,
    ) -> CaseRef: ...

    def propose(
        self,
        case_id: str,
        *,
        kind: str,
        payload: dict[str, Any],
        proposed_by: str,
        proposer_kind: str,
        required_permission: Permission | None = None,
        on_behalf_of: str | None = None,
    ) -> Any: ...
