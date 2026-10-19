"""Event log, broker and consumers.

``EventLog`` is the append-only, hash-chained log every state change lands in (Kafka topic
compaction disabled + WORM tiering in production). ``Broker`` delivers to subscribers
at-least-once; ``IdempotentConsumer`` turns that into effectively-once processing.
The Kafka/Redpanda adapter implements the same ``Publisher`` port.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol

from platform_core.canonical import canonical_json, sha256_hex
from platform_core.errors import IntegrityError

from events.envelope import EventEnvelope

log = logging.getLogger(__name__)
GENESIS = "0" * 64


class Publisher(Protocol):
    def publish(self, event: EventEnvelope) -> None: ...


@dataclass(frozen=True)
class LoggedEvent:
    offset: int
    event: EventEnvelope
    prev_hash: str
    hash: str


class EventLog:
    """Append-only, hash-chained. Partition order is preserved by obligor (partition key)."""

    def __init__(self) -> None:
        self._entries: list[LoggedEvent] = []
        self._ids: set[str] = set()

    def append(self, event: EventEnvelope) -> LoggedEvent:
        if event.id in self._ids:
            existing = next(e for e in self._entries if e.event.id == event.id)
            if existing.event != event:
                raise IntegrityError(f"event id {event.id} reused with different content")
            return existing  # redelivery of the same event: idempotent
        prev = self._entries[-1].hash if self._entries else GENESIS
        h = sha256_hex(prev.encode() + canonical_json(event.model_dump(mode="json")))
        entry = LoggedEvent(len(self._entries), event, prev, h)
        self._entries.append(entry)
        self._ids.add(event.id)
        return entry

    def __len__(self) -> int:
        return len(self._entries)

    def read(
        self,
        *,
        types: set[str] | None = None,
        obligor_id: str | None = None,
        valid_to: date | None = None,
        known_at: datetime | None = None,
    ) -> Iterator[EventEnvelope]:
        """Replay view: events known by ``known_at`` with business date <= ``valid_to``."""
        for e in self._entries:
            ev = e.event
            if types and ev.type not in types:
                continue
            if obligor_id and ev.subject.obligor_id != obligor_id:
                continue
            if valid_to and ev.valid_at > valid_to:
                continue
            if known_at and ev.recorded_at > known_at:
                continue
            yield ev

    def verify(self) -> None:
        prev = GENESIS
        for e in self._entries:
            if e.prev_hash != prev:
                raise IntegrityError(f"event log chain broken at offset {e.offset}")
            if (
                sha256_hex(prev.encode() + canonical_json(e.event.model_dump(mode="json")))
                != e.hash
            ):
                raise IntegrityError(f"event at offset {e.offset} altered")
            prev = e.hash

    @property
    def head(self) -> str:
        return self._entries[-1].hash if self._entries else GENESIS


Handler = Callable[[EventEnvelope], None]


@dataclass
class DeadLetter:
    event: EventEnvelope
    consumer: str
    error: str
    attempts: int


@dataclass
class Broker:
    """In-process broker with the Kafka semantics we rely on: per-type topics, consumer
    groups, at-least-once delivery, DLQ after ``max_attempts``."""

    log: EventLog = field(default_factory=EventLog)
    max_attempts: int = 3
    redeliver_all: bool = False  # chaos switch: deliver every event twice
    dlq: list[DeadLetter] = field(default_factory=list)
    _subs: dict[str, list[tuple[str, Handler]]] = field(default_factory=lambda: defaultdict(list))

    def subscribe(self, event_type: str, consumer_name: str, handler: Handler) -> None:
        self._subs[event_type].append((consumer_name, handler))

    def publish(self, event: EventEnvelope) -> None:
        self.log.append(event)
        deliveries = 2 if self.redeliver_all else 1
        for consumer_name, handler in list(self._subs.get(event.type, [])) + list(
            self._subs.get("*", [])
        ):
            for _ in range(deliveries):
                self._deliver(consumer_name, handler, event)

    def _deliver(self, consumer_name: str, handler: Handler, event: EventEnvelope) -> None:
        last: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                handler(event)
                return
            except Exception as exc:  # noqa: BLE001 - consumers never swallow; we DLQ
                last = exc
                log.warning(
                    "consumer %s failed attempt %s on %s", consumer_name, attempt, event.type
                )
        self.dlq.append(DeadLetter(event, consumer_name, repr(last), self.max_attempts))


class IdempotentConsumer:
    """Base for consumers. ``processed`` is stored in the consumer's own DB in production."""

    name = "consumer"

    def __init__(self) -> None:
        self.processed: set[str] = set()
        self.duplicates_skipped = 0

    def __call__(self, event: EventEnvelope) -> None:
        if event.id in self.processed:
            self.duplicates_skipped += 1
            return
        self.handle(event)
        self.processed.add(event.id)  # mark only after successful handling

    def handle(self, event: EventEnvelope) -> None:  # pragma: no cover - abstract
        raise NotImplementedError


# ---------------------------------------------------------------- transactional outbox ---
class SqliteOutbox:
    """Transactional outbox on SQLite (Postgres in shared envs, same schema).

    Business rows and the outbox row commit in one transaction; ``relay`` publishes pending
    rows in order and marks them dispatched only after the broker accepts them.
    """

    def __init__(self, path: str = ":memory:") -> None:
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS outbox (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
            "event_id TEXT UNIQUE NOT NULL, body TEXT NOT NULL, "
            "dispatched INTEGER NOT NULL DEFAULT 0)"
        )

    @contextmanager
    def transaction(self) -> Iterator[OutboxTx]:
        self.conn.execute("BEGIN")
        tx = OutboxTx(self.conn)
        try:
            yield tx
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def pending(self) -> int:
        return int(
            self.conn.execute("SELECT COUNT(*) FROM outbox WHERE dispatched=0").fetchone()[0]
        )

    def relay(self, publisher: Publisher, batch: int = 500) -> int:
        rows = self.conn.execute(
            "SELECT seq, body FROM outbox WHERE dispatched=0 ORDER BY seq LIMIT ?", (batch,)
        ).fetchall()
        sent = 0
        for seq, body in rows:
            publisher.publish(EventEnvelope.model_validate_json(body))
            self.conn.execute("UPDATE outbox SET dispatched=1 WHERE seq=?", (seq,))
            sent += 1
        return sent


class OutboxTx:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def add_event(self, event: EventEnvelope) -> None:
        self.conn.execute(
            "INSERT INTO outbox(event_id, body) VALUES (?, ?)", (event.id, event.model_dump_json())
        )


def replay(
    log_: EventLog,
    handler: Handler,
    *,
    types: set[str] | None = None,
    obligor_id: str | None = None,
    valid_to: date | None = None,
    known_at: datetime | None = None,
) -> int:
    n = 0
    for ev in log_.read(types=types, obligor_id=obligor_id, valid_to=valid_to, known_at=known_at):
        handler(ev)
        n += 1
    return n
