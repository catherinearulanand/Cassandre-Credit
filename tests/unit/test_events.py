from __future__ import annotations

import json
from datetime import date

import pytest
from events import Subject, make_event
from events.bus import Broker, EventLog, IdempotentConsumer, SqliteOutbox, replay
from events.catalogue import UnknownEventType
from events.compat import breaking_changes, check, current_schemas, export
from platform_core.errors import IntegrityError


def _ev(clock, obligor="OBL-1", valid_at=date(2026, 9, 30), node="LIM-1", new=100):  # type: ignore[no-untyped-def]
    return make_event(
        clock=clock,
        type="limit.changed",
        source="limit-service",
        subject=Subject(obligor_id=obligor),
        correlation_id="c1",
        valid_at=valid_at,
        data={
            "node_id": node,
            "old_sanctioned_paise": 0,
            "new_sanctioned_paise": new,
            "reason": "test",
        },
    )


def test_payload_validated(clock) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(Exception):
        make_event(
            clock=clock,
            type="limit.changed",
            source="x",
            subject=Subject(obligor_id="O"),
            correlation_id="c",
            data={"node_id": "n"},
        )
    with pytest.raises(UnknownEventType):
        make_event(
            clock=clock,
            type="nope.happened",
            source="x",
            subject=Subject(obligor_id="O"),
            correlation_id="c",
            data={},
        )


def test_event_log_hash_chain(clock) -> None:  # type: ignore[no-untyped-def]
    log = EventLog()
    for i in range(5):
        log.append(_ev(clock, new=i))
    log.verify()
    e = log._entries[2]
    forged = e.event.model_copy(update={"data": {**e.event.data, "new_sanctioned_paise": 999}})
    log._entries[2] = type(e)(e.offset, forged, e.prev_hash, e.hash)
    with pytest.raises(IntegrityError):
        log.verify()


class Counter(IdempotentConsumer):
    name = "counter"

    def __init__(self) -> None:
        super().__init__()
        self.total = 0

    def handle(self, event) -> None:  # type: ignore[no-untyped-def]
        self.total += event.data["new_sanctioned_paise"]


def test_duplicate_delivery_processed_once(clock) -> None:  # type: ignore[no-untyped-def]
    broker = Broker(redeliver_all=True)
    c = Counter()
    broker.subscribe("limit.changed", c.name, c)
    for v in (10, 20, 30):
        broker.publish(_ev(clock, new=v))
    assert c.total == 60
    assert c.duplicates_skipped == 3


def test_failing_consumer_goes_to_dlq(clock) -> None:  # type: ignore[no-untyped-def]
    broker = Broker()

    def boom(_):  # type: ignore[no-untyped-def]
        raise RuntimeError("bad")

    broker.subscribe("limit.changed", "boom", boom)
    broker.publish(_ev(clock))
    assert len(broker.dlq) == 1 and broker.dlq[0].attempts == 3


def test_outbox_atomic_with_business_write(clock) -> None:  # type: ignore[no-untyped-def]
    ob = SqliteOutbox()
    ob.conn.execute("CREATE TABLE limits(id TEXT PRIMARY KEY, amt INTEGER)")
    with ob.transaction() as tx:
        tx.execute("INSERT INTO limits VALUES (?, ?)", ("L1", 100))
        tx.add_event(_ev(clock))
    with pytest.raises(RuntimeError), ob.transaction() as tx:
        tx.execute("INSERT INTO limits VALUES (?, ?)", ("L2", 200))
        tx.add_event(_ev(clock))
        raise RuntimeError("crash before commit")
    assert ob.conn.execute("SELECT COUNT(*) FROM limits").fetchone()[0] == 1
    assert ob.pending() == 1
    broker = Broker()
    assert ob.relay(broker) == 1 and ob.pending() == 0 and len(broker.log) == 1


def test_outbox_relay_survives_publisher_failure(clock) -> None:  # type: ignore[no-untyped-def]
    ob = SqliteOutbox()
    with ob.transaction() as tx:
        tx.add_event(_ev(clock))

    class Down:
        def publish(self, e):  # type: ignore[no-untyped-def]
            raise ConnectionError("kafka down")

    with pytest.raises(ConnectionError):
        ob.relay(Down())
    assert ob.pending() == 1  # nothing lost


def test_replay_rebuilds_state_as_of(clock) -> None:  # type: ignore[no-untyped-def]
    log = EventLog()
    log.append(_ev(clock, valid_at=date(2026, 6, 30), new=100))
    t_before = clock.now()
    clock.advance(days=1)
    log.append(_ev(clock, valid_at=date(2026, 9, 30), new=250))
    state: dict[str, int] = {}

    def apply(e):  # type: ignore[no-untyped-def]
        state[e.data["node_id"]] = e.data["new_sanctioned_paise"]

    replay(log, apply, valid_to=date(2026, 7, 31))
    assert state["LIM-1"] == 100
    state.clear()
    replay(log, apply)
    assert state["LIM-1"] == 250
    state.clear()
    replay(log, apply, known_at=t_before)
    assert state["LIM-1"] == 100


def test_contracts_committed_and_compatible() -> None:
    assert check() == [], "run: python -m tools.cli contracts-export after an intended change"
    assert len(current_schemas()) >= 28


def test_compat_detects_breaking_change() -> None:
    old = {"properties": {"a": {"type": "string"}, "b": {"type": "integer"}}, "required": ["a"]}
    assert breaking_changes(old, {"properties": {"a": {"type": "string"}}, "required": ["a"]}) == [
        "b: removed"
    ]
    assert breaking_changes(
        old, {"properties": {**old["properties"], "c": {"type": "string"}}, "required": ["a", "c"]}
    ) == ["c: new required field"]
    assert breaking_changes(
        old, {"properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}, "required": ["a"]}
    ) == ["a: type changed"]
    assert (
        breaking_changes(
            old, {"properties": {**old["properties"], "c": {"type": "string"}}, "required": ["a"]}
        )
        == []
    )


def test_seeded_breaking_change_fails_check(tmp_path) -> None:  # type: ignore[no-untyped-def]
    export(tmp_path)
    p = tmp_path / "limit.changed.v1.json"
    schema = json.loads(p.read_text())
    schema["properties"]["legacy_field"] = {"type": "string"}  # code no longer produces it
    p.write_text(json.dumps(schema))
    assert any("legacy_field: removed" in x for x in check(tmp_path))
