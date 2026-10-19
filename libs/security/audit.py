"""Append-only, hash-chained audit log.

Each entry stores ``prev_hash`` and ``entry_hash = sha256(prev_hash || canonical(entry))``.
Any edit, deletion or reorder breaks verification from that point on. Periodic anchors
(the head hash, signed) can be written to WORM storage so even a full rewrite is detectable.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from platform_core.canonical import canonical_json, sha256_hex
from platform_core.clock import Clock
from platform_core.errors import IntegrityError
from platform_core.ids import new_id

from security.crypto import Signature, Signer

GENESIS = "0" * 64


@dataclass(frozen=True)
class AuditEntry:
    seq: int
    entry_id: str
    at: datetime
    actor_kind: str  # human | service | agent
    actor_id: str
    on_behalf_of: str | None
    action: str
    subject: str
    detail: dict[str, Any]
    prev_hash: str
    entry_hash: str

    def body(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "entry_id": self.entry_id,
            "at": self.at.isoformat(),
            "actor_kind": self.actor_kind,
            "actor_id": self.actor_id,
            "on_behalf_of": self.on_behalf_of,
            "action": self.action,
            "subject": self.subject,
            "detail": self.detail,
        }


class AuditLog:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._entries: list[AuditEntry] = []

    @property
    def head(self) -> str:
        return self._entries[-1].entry_hash if self._entries else GENESIS

    def append(
        self,
        *,
        actor_kind: str,
        actor_id: str,
        action: str,
        subject: str,
        detail: dict[str, Any] | None = None,
        on_behalf_of: str | None = None,
    ) -> AuditEntry:
        if actor_kind not in ("human", "service", "agent"):
            raise ValueError("actor_kind must be human, service or agent")
        if actor_kind == "agent" and not on_behalf_of:
            raise ValueError("agent actions must name the human principal (on_behalf_of)")
        prev = self.head
        draft = AuditEntry(
            seq=len(self._entries),
            entry_id=new_id("AUD"),
            at=self._clock.now(),
            actor_kind=actor_kind,
            actor_id=actor_id,
            on_behalf_of=on_behalf_of,
            action=action,
            subject=subject,
            detail=copy.deepcopy(detail) if detail else {},
            prev_hash=prev,
            entry_hash="",
        )
        h = sha256_hex(prev.encode() + canonical_json(draft.body()))
        entry = AuditEntry(**{**draft.__dict__, "entry_hash": h})
        self._entries.append(entry)
        return entry

    def entries(self) -> list[AuditEntry]:
        return list(self._entries)

    def verify(self) -> None:
        prev = GENESIS
        for i, e in enumerate(self._entries):
            if e.seq != i or e.prev_hash != prev:
                raise IntegrityError(f"audit chain broken at seq {i}")
            if sha256_hex(prev.encode() + canonical_json(e.body())) != e.entry_hash:
                raise IntegrityError(f"audit entry {i} altered")
            prev = e.entry_hash

    def anchor(self, signer: Signer) -> Signature:
        """Sign the current head for export to WORM storage."""
        self.verify()
        return signer.sign_hash(self.head)

    # test helper to simulate tampering by an insider with DB access
    def _tamper(self, seq: int, **changes: Any) -> None:
        e = self._entries[seq]
        self._entries[seq] = AuditEntry(**{**e.__dict__, **changes})
