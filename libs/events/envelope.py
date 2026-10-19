"""CloudEvents-compatible envelope (Section 7.4). Bitemporal: valid_at + occurred/recorded."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from platform_core.clock import Clock
from platform_core.ids import new_event_id
from pydantic import BaseModel, ConfigDict, Field


class Subject(BaseModel):
    model_config = ConfigDict(frozen=True)

    obligor_id: str
    cts_id: str | None = None
    cts_version: int | None = None
    entity_id: str | None = None


class Actor(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["service", "human", "agent"]
    id: str
    on_behalf_of: str | None = None


class EventEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    type: str
    schema_version: int = 1
    source: str
    occurred_at: datetime
    recorded_at: datetime
    valid_at: date
    subject: Subject
    correlation_id: str
    causation_id: str | None = None
    actor: Actor
    data: dict[str, Any]
    lineage: list[str] = Field(default_factory=list)

    @property
    def partition_key(self) -> str:
        return self.subject.obligor_id


def make_event(
    *,
    clock: Clock,
    type: str,
    source: str,
    subject: Subject,
    data: dict[str, Any],
    correlation_id: str,
    valid_at: date | None = None,
    causation_id: str | None = None,
    actor: Actor | None = None,
    lineage: list[str] | None = None,
    schema_version: int = 1,
    validate: bool = True,
) -> EventEnvelope:
    if validate:
        from events.catalogue import validate_payload

        data = validate_payload(type, schema_version, data)
    now = clock.now()
    if actor is not None and actor.kind == "agent" and not actor.on_behalf_of:
        raise ValueError("agent-originated events must carry on_behalf_of")
    return EventEnvelope(
        id=new_event_id(),
        type=type,
        schema_version=schema_version,
        source=source,
        occurred_at=now,
        recorded_at=now,
        valid_at=valid_at or now.date(),
        subject=subject,
        correlation_id=correlation_id,
        causation_id=causation_id,
        actor=actor or Actor(kind="service", id=source),
        data=data,
        lineage=lineage or [],
    )
