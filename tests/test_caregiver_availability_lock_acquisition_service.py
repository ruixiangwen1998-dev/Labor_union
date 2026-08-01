import json
from datetime import date, timedelta

import pytest

from services import caregiver_availability_lock_service as service
from services.caregiver_availability_lock_acquisition_helpers import (
    build_acquired_event_payload,
    normalize_plan_snapshot,
)


def _segments(count):
    start = date(2026, 8, 1)
    rows = []
    for index in range(count):
        rows.append(
            {
                "id": 11 + index,
                "plan_id": 7,
                "segment_order": index + 1,
                "staff_id": 3 + index,
                "assigned_start_date": start + timedelta(days=index),
                "assigned_end_date": start + timedelta(days=index),
            }
        )
    return rows


class _Cursor:
    def __init__(self, *, count=1, conflict=None, existing=False, plan_status=None):
        self.count = count
        self.segments = _segments(count)
        self.conflict = conflict
        self.existing = existing
        self.plan_status = plan_status or ("accepted" if existing else "proposed")
        self.sql = ""
        self.executed = []
        self.lastrowid = 88
        self.rowcount = 1
        self.closed = False
        self.fail_execute_at = None
        self.fail_fetch_kind = None

    @property
    def end(self):
        return date(2026, 8, self.count)

    @property
    def snapshot(self):
        return normalize_plan_snapshot(
            "C-1",
            7,
            {
                "id": 7,
                "case_no": "C-1",
                "status": self.plan_status,
                "is_active": 1,
                "start_date": date(2026, 8, 1),
                "end_date": self.end,
            },
            self.segments,
        )

    def execute(self, sql, params=()):
        self.sql = sql
        self.executed.append((sql, params))
        if self.fail_execute_at == len(self.executed):
            raise RuntimeError("execute failed")

    def fetchone(self):
        if self.fail_fetch_kind == "one":
            raise RuntimeError("fetchone failed")
        if "FROM caregiver_matching_plans" in self.sql:
            return {
                "id": 7,
                "case_no": "C-1",
                "status": self.plan_status,
                "is_active": 1,
                "start_date": date(2026, 8, 1),
                "end_date": self.end,
            }
        if "FROM orders" in self.sql:
            return {
                "case_no": "C-1",
                "status": "洽談中",
                "start_date": date(2026, 8, 1),
                "end_date": self.end,
            }
        if "FROM caregiver_availability_lock_events" in self.sql:
            if not self.existing:
                return None
            payload = build_acquired_event_payload(
                {
                    "case_no": "C-1",
                    "plan_id": 7,
                    "event_key": "event-1",
                    "actor": "admin",
                    "lock_id": 88,
                },
                self.snapshot,
            )
            return {
                "id": 90,
                "lock_id": 88,
                "event_type": "lock_acquired",
                "event_key": "event-1",
                "actor": "admin",
                "reason": None,
                "payload": json.dumps(payload),
            }
        if "FROM caregiver_availability_locks WHERE id" in self.sql:
            return {"id": 88, "plan_id": 7, "status": "active", "is_active": 1}
        return None

    def fetchall(self):
        if self.fail_fetch_kind == "all":
            raise RuntimeError("fetchall failed")
        if "FROM caregiver_matching_plan_segments" in self.sql:
            return [dict(row) for row in self.segments]
        if "FROM case_staff_assignments" in self.sql:
            if self.conflict != "assignment":
                return []
            return [
                {
                    "source_id": 17,
                    "staff_id": 3,
                    "assigned_start_date": date(2026, 8, 1),
                    "assigned_end_date": date(2026, 8, 1),
                }
            ]
        if "FROM staff_schedule" in self.sql:
            if self.conflict not in {"owned_schedule", "legacy_schedule"}:
                return []
            return [
                {
                    "source_id": 18,
                    "staff_id": 3,
                    "work_date": date(2026, 8, 1),
                    "assignment_id": 17 if self.conflict == "owned_schedule" else None,
                }
            ]
        if "SELECT l.id FROM caregiver_availability_locks" in self.sql:
            return [{"id": 88}] if self.conflict == "active_lock" or self.existing else []
        if "SELECT d.id AS source_id" in self.sql:
            if self.conflict != "active_lock" and not self.existing:
                return []
            return [
                {
                    "source_id": 19,
                    "lock_id": 88,
                    "staff_id": 3,
                    "lock_date": date(2026, 8, 1),
                }
            ]
        if "SELECT segment_id, staff_id, lock_date" in self.sql:
            return [
                {
                    "segment_id": row["segment_id"],
                    "staff_id": row["staff_id"],
                    "lock_date": date.fromisoformat(row["lock_date"]),
                }
                for row in self.snapshot["lock_rows"]
            ]
        raise AssertionError(self.sql)

    def close(self):
        self.closed = True


class _Connection:
    def __init__(self, cursor):
        self.cursor_value = cursor
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.fail_cursor = False
        self.fail_commit = False
        self.fail_rollback = False
        self.fail_close = False

    def cursor(self):
        if self.fail_cursor:
            raise RuntimeError("cursor failed")
        return self.cursor_value

    def commit(self):
        self.commits += 1
        if self.fail_commit:
            raise RuntimeError("commit failed")

    def rollback(self):
        self.rollbacks += 1
        if self.fail_rollback:
            raise RuntimeError("rollback failed")

    def close(self):
        self.closed = True
        if self.fail_close:
            raise RuntimeError("close failed")


def _install(monkeypatch, **cursor_options):
    cursor = _Cursor(**cursor_options)
    connection = _Connection(cursor)
    mutex_calls = []
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    def mutex(actual_cursor, ids):
        mutex_calls.append((actual_cursor, ids))
        return sorted(ids)

    monkeypatch.setattr(service, "lock_staff_occupancy_mutex", mutex)
    return connection, cursor, mutex_calls


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_created_for_one_through_four_segments(monkeypatch, count):
    connection, cursor, mutex_calls = _install(monkeypatch, count=count)

    result = service.acquire_caregiver_availability_lock("C-1", 7, "event-1", "admin")

    assert result["result"] == "created"
    assert result["lock_rows"] == cursor.snapshot["lock_rows"]
    assert mutex_calls == [(cursor, list(range(3, 3 + count)))]
    assert connection.commits == 1
    assert connection.rollbacks == 0
    writes = [(sql, params) for sql, params in cursor.executed if sql.startswith(("INSERT", "UPDATE"))]
    assert len([sql for sql, _ in writes if "lock_days" in sql]) == count
    assert "status = 'accepted'" in writes[-2][0]
    assert "status = 'proposed'" in writes[-2][0]
    assert "lock_acquired" in writes[-1][0]


def test_exact_replay_returns_existing_and_reuses_event(monkeypatch):
    connection, cursor, mutex_calls = _install(monkeypatch, existing=True)

    first = service.acquire_caregiver_availability_lock("C-1", 7, "event-1", "admin")
    second_connection, second_cursor, _ = _install(monkeypatch, existing=True)
    second = service.acquire_caregiver_availability_lock("C-1", 7, "event-1", "admin")

    assert first == second
    assert first["result"] == "existing"
    assert mutex_calls == [(cursor, [3])]
    assert connection.commits == second_connection.commits == 0
    assert not [sql for sql, _ in cursor.executed if sql.startswith(("INSERT", "UPDATE", "DELETE"))]
    assert not [sql for sql, _ in second_cursor.executed if sql.startswith(("INSERT", "UPDATE", "DELETE"))]


def test_event_key_reuse_with_different_actor_fails_closed(monkeypatch):
    connection, cursor, _ = _install(monkeypatch, existing=True)
    with pytest.raises(ValueError, match="already been used"):
        service.acquire_caregiver_availability_lock("C-1", 7, "event-1", "other-admin")
    assert connection.rollbacks == 1
    assert not [sql for sql, _ in cursor.executed if sql.startswith(("INSERT", "UPDATE", "DELETE"))]


def test_cross_case_plan_and_duplicate_segments_fail_closed(monkeypatch):
    connection, cursor, _ = _install(monkeypatch)
    with pytest.raises(ValueError, match="case_no"):
        service.acquire_caregiver_availability_lock("C-2", 7, "event-1", "admin")
    assert connection.rollbacks == 1

    connection2, cursor2, _ = _install(monkeypatch, count=2)
    cursor2.segments[1]["segment_order"] = 1
    with pytest.raises(ValueError):
        service.acquire_caregiver_availability_lock("C-1", 7, "event-1", "admin")
    assert connection2.rollbacks == 1


@pytest.mark.parametrize(
    ("conflict", "source_type", "source_id"),
    [
        ("assignment", "assignment", 17),
        ("owned_schedule", "schedule", 18),
        ("legacy_schedule", "schedule", 18),
        ("active_lock", "active_lock", 19),
    ],
)
def test_each_occupancy_conflict_rolls_back_without_writes(
    monkeypatch, conflict, source_type, source_id
):
    connection, cursor, _ = _install(monkeypatch, conflict=conflict)

    with pytest.raises(ValueError) as raised:
        service.acquire_caregiver_availability_lock("C-1", 7, "event-1", "admin")

    payload = json.loads(str(raised.value))
    assert payload == {
        "conflicts": [
            {
                "lock_date": "2026-08-01",
                "source_id": source_id,
                "source_type": source_type,
                "staff_id": 3,
            }
        ]
    }
    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert not [sql for sql, _ in cursor.executed if sql.startswith(("INSERT", "UPDATE", "DELETE"))]


def test_fixed_lock_order_mutex_before_occupancy_and_writes(monkeypatch):
    _, cursor, mutex_calls = _install(monkeypatch)
    service.acquire_caregiver_availability_lock("C-1", 7, "event-1", "admin")
    sql = [statement for statement, _ in cursor.executed]

    order_position = next(i for i, statement in enumerate(sql) if "FROM orders" in statement)
    expected = [
        "FROM case_staff_assignments",
        "FROM staff_schedule",
        "SELECT l.id FROM caregiver_availability_locks",
        "SELECT d.id AS source_id",
        "FROM caregiver_availability_lock_events",
        "INSERT INTO caregiver_availability_locks",
        "INSERT INTO caregiver_availability_lock_days",
        "UPDATE caregiver_matching_plans",
        "INSERT INTO caregiver_availability_lock_events",
    ]
    positions = [order_position]
    positions.append(
        next(
            i
            for i, statement in enumerate(sql)
            if i > order_position and "FROM caregiver_matching_plans" in statement
        )
    )
    positions.append(
        next(
            i
            for i, statement in enumerate(sql)
            if i > order_position and "FROM caregiver_matching_plan_segments" in statement
        )
    )
    positions.extend(
        next(i for i, statement in enumerate(sql) if i > order_position and token in statement)
        for token in expected
    )
    assert positions == sorted(positions)
    assert mutex_calls == [(cursor, [3])]
    assert all("FOR UPDATE" in sql[i] for i in positions[:8])
    assert not any(
        statement.startswith(("INSERT", "UPDATE", "DELETE"))
        for statement in sql[: positions[3]]
    )
    assert not any(
        token in " ".join(sql).upper()
        for token in ("COUNT(", "SUM(", "MAX(", "EXISTS")
    )


def test_mutex_mismatch_and_mutex_exception_fail_closed(monkeypatch):
    connection, cursor, _ = _install(monkeypatch)
    monkeypatch.setattr(service, "lock_staff_occupancy_mutex", lambda *_: [])
    with pytest.raises(ValueError, match="mutex"):
        service.acquire_caregiver_availability_lock("C-1", 7, "event-1", "admin")
    assert connection.rollbacks == 1
    assert not [sql for sql, _ in cursor.executed if sql.startswith(("INSERT", "UPDATE"))]

    connection2, _, _ = _install(monkeypatch)
    monkeypatch.setattr(
        service,
        "lock_staff_occupancy_mutex",
        lambda *_: (_ for _ in ()).throw(RuntimeError("mutex failed")),
    )
    with pytest.raises(RuntimeError, match="mutex failed"):
        service.acquire_caregiver_availability_lock("C-1", 7, "event-1", "admin")
    assert connection2.rollbacks == 1


@pytest.mark.parametrize("status", ["draft", "rejected", "superseded", "cancelled", "accepted"])
def test_non_proposed_plan_fails_without_writes(monkeypatch, status):
    connection, cursor, _ = _install(monkeypatch, plan_status=status)
    with pytest.raises(ValueError, match="active proposed"):
        service.acquire_caregiver_availability_lock("C-1", 7, "event-1", "admin")
    assert connection.rollbacks == 1
    assert not [sql for sql, _ in cursor.executed if sql.startswith(("INSERT", "UPDATE"))]


@pytest.mark.parametrize("fail_at", range(1, 15))
def test_each_execute_failure_rolls_back_and_closes(monkeypatch, fail_at):
    connection, cursor, _ = _install(monkeypatch)
    cursor.fail_execute_at = fail_at
    with pytest.raises(RuntimeError, match="execute failed"):
        service.acquire_caregiver_availability_lock("C-1", 7, "event-1", "admin")
    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert cursor.closed and connection.closed


@pytest.mark.parametrize("fetch_kind", ["one", "all"])
def test_fetch_failures_rollback_and_close(monkeypatch, fetch_kind):
    connection, cursor, _ = _install(monkeypatch)
    cursor.fail_fetch_kind = fetch_kind
    with pytest.raises(RuntimeError, match="fetch"):
        service.acquire_caregiver_availability_lock("C-1", 7, "event-1", "admin")
    assert connection.rollbacks == 1
    assert cursor.closed and connection.closed


def test_connection_cursor_commit_and_cleanup_exceptions(monkeypatch):
    monkeypatch.setattr(service, "get_connection", lambda: (_ for _ in ()).throw(RuntimeError("connect")))
    with pytest.raises(RuntimeError, match="connect"):
        service.acquire_caregiver_availability_lock("C-1", 7, "event-1", "admin")

    connection, _, _ = _install(monkeypatch)
    connection.fail_cursor = True
    with pytest.raises(RuntimeError, match="cursor"):
        service.acquire_caregiver_availability_lock("C-1", 7, "event-1", "admin")
    assert connection.rollbacks == 1 and connection.closed

    connection2, cursor2, _ = _install(monkeypatch)
    connection2.fail_commit = connection2.fail_rollback = connection2.fail_close = True
    cursor2.close = lambda: (_ for _ in ()).throw(RuntimeError("cursor close"))
    with pytest.raises(RuntimeError, match="commit"):
        service.acquire_caregiver_availability_lock("C-1", 7, "event-1", "admin")
    assert connection2.commits == 1 and connection2.rollbacks == 1
    assert connection2.closed


def test_malformed_rows_and_stale_snapshot_fail_closed(monkeypatch):
    connection, cursor, _ = _install(monkeypatch)
    cursor.segments[0]["unexpected"] = 1
    with pytest.raises(ValueError):
        service.acquire_caregiver_availability_lock("C-1", 7, "event-1", "admin")
    assert connection.rollbacks == 1

    connection2, cursor2, _ = _install(monkeypatch)
    original = cursor2.fetchall
    calls = {"segments": 0}

    def stale():
        rows = original()
        if "FROM caregiver_matching_plan_segments" in cursor2.sql:
            calls["segments"] += 1
            if calls["segments"] == 2:
                rows[0]["staff_id"] = 99
        return rows

    cursor2.fetchall = stale
    with pytest.raises(ValueError, match="changed"):
        service.acquire_caregiver_availability_lock("C-1", 7, "event-1", "admin")
    assert connection2.rollbacks == 1


def test_tuple_row_and_duplicate_active_header_join_rows(monkeypatch):
    connection, cursor, _ = _install(monkeypatch)
    cursor.fetchone = lambda: (7, "C-1")
    with pytest.raises(ValueError, match="matching plan"):
        service.acquire_caregiver_availability_lock("C-1", 7, "event-1", "admin")
    assert connection.rollbacks == 1

    connection2, cursor2, _ = _install(monkeypatch, conflict="active_lock")
    original = cursor2.fetchall

    def duplicate_join_rows():
        rows = original()
        if "SELECT l.id FROM caregiver_availability_locks" in cursor2.sql:
            return rows + rows
        return rows

    cursor2.fetchall = duplicate_join_rows
    with pytest.raises(ValueError) as raised:
        service.acquire_caregiver_availability_lock("C-1", 7, "event-1", "admin")
    assert json.loads(str(raised.value))["conflicts"][0]["source_type"] == "active_lock"
    assert connection2.rollbacks == 1
