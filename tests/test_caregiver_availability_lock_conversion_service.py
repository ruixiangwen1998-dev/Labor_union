from datetime import date
from decimal import Decimal

import pytest

from services import caregiver_availability_lock_conversion_service as service


def terms(count=1, *, floor_fee="0.00"):
    return [
        {"segment_id": 10 + index, "hourly_rate": Decimal("300.00"), "floor_fee_allocated": Decimal(floor_fee if index == 0 else "0.00")}
        for index in range(count)
    ]


class TransactionCursor:
    def __init__(self, *, insert_rowcount=1):
        self._insert_rowcount = insert_rowcount
        self.lock_day_count = 1
        self.rowcount = insert_rowcount
        self.lastrowid = 101
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        self.rowcount = (
            self.lock_day_count
            if "UPDATE caregiver_availability_lock_days" in sql
            else self._insert_rowcount
        )

    def close(self):
        self.calls.append(("CURSOR_CLOSE", ()))


class TransactionConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


def _install_conversion_path(
    monkeypatch,
    cursor,
    connection,
    *,
    actual_hours=Decimal("8.00"),
    calendar_days=1,
    target_hours=Decimal("8.00"),
):
    segment_end = date(2026, 8, 2 + calendar_days)
    lock_days = [
        {
            "id": 30 + offset,
            "segment_id": 10,
            "staff_id": 20,
            "lock_date": date(2026, 8, 3 + offset),
            "active_marker": 1,
            "released_by": None,
            "released_at": None,
        }
        for offset in range(calendar_days)
    ]
    lock_calls = []
    cursor.lock_day_count = calendar_days

    def load_days(passed_cursor, lock_id, *, active_only=True, for_update=True):
        lock_calls.append((passed_cursor, lock_id, active_only, for_update))
        return [dict(row) for row in lock_days]

    monkeypatch.setattr(service, "get_connection", lambda: connection)
    monkeypatch.setattr(service, "_load_preflight_lock_days", load_days)
    monkeypatch.setattr(service, "lock_staff_occupancy_mutex", lambda passed_cursor, ids: list(ids))
    monkeypatch.setattr(service, "_existing_result", lambda *_: None)
    monkeypatch.setattr(
        service,
        "_load_state",
        lambda *_: {
            "current_date": date(2026, 8, 1),
            "order": {
                "start_date": date(2026, 8, 3),
                "end_date": segment_end,
                "service_days": int(target_hours / Decimal("8.00")),
                "daily_hours": Decimal("8.00"),
                "target_hours": target_hours,
                "floor_fee": Decimal("0.00"),
            },
            "plan_id": 3,
            "lock_days": lock_days,
            "snapshot": {"segments": [{
                "segment_id": 10, "segment_order": 1, "staff_id": 20,
                "assigned_start_date": date(2026, 8, 3),
                "assigned_end_date": segment_end,
            }]},
        },
    )
    monkeypatch.setattr(service, "validate_assignment_plan_transition", lambda **_: None)
    monkeypatch.setattr(
        service,
        "generate_assignment_schedule_in_transaction",
        lambda *_: {
            "assignment_schedule": [{
                "work_date": "2026-08-03", "is_work_day": 1, "is_double_pay": 0, "notes": None,
            }],
            "actual_hours": actual_hours,
        },
    )
    return lock_calls


def test_public_service_acquires_mutex_before_for_update_and_commits_once(monkeypatch):
    cursor = TransactionCursor()
    connection = TransactionConnection(cursor)
    lock_calls = _install_conversion_path(monkeypatch, cursor, connection)

    result = service.convert_availability_lock_to_assignments(
        "C-1", 7, "event-1", "admin", "paid", terms(),
    )

    assert result["result"] == "created"
    assert [call[3] for call in lock_calls[:2]] == [False, True]
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert any(
        "UPDATE orders SET status = '訂單成立'" in statement
        for statement, _ in cursor.calls
    )


def test_public_service_rolls_back_when_actual_hours_do_not_reconcile(monkeypatch):
    cursor = TransactionCursor()
    connection = TransactionConnection(cursor)
    _install_conversion_path(monkeypatch, cursor, connection, actual_hours=Decimal("7.00"))

    with pytest.raises(ValueError, match="accounting totals"):
        service.convert_availability_lock_to_assignments(
            "C-1", 7, "event-1", "admin", "paid", terms(),
        )

    assert connection.commits == 0
    assert connection.rollbacks == 1

def test_conversion_conserves_order_target_without_copying_planned_hours(monkeypatch):
    cursor = TransactionCursor()
    connection = TransactionConnection(cursor)
    _install_conversion_path(
        monkeypatch,
        cursor,
        connection,
        actual_hours=Decimal("8.00"),
        calendar_days=2,
        target_hours=Decimal("8.00"),
    )

    result = service.convert_availability_lock_to_assignments(
        "C-1", 7, "event-1", "admin", "paid", terms(),
    )

    assert result["planned_hours"] == Decimal("16.00")
    assert result["actual_hours"] == Decimal("8.00")
    assert connection.commits == 1


def test_public_service_rolls_back_on_assignment_insert_rowcount(monkeypatch):
    cursor = TransactionCursor(insert_rowcount=0)
    connection = TransactionConnection(cursor)
    _install_conversion_path(monkeypatch, cursor, connection)

    with pytest.raises(ValueError, match="assignment insert rowcount"):
        service.convert_availability_lock_to_assignments(
            "C-1", 7, "event-1", "admin", "paid", terms(),
        )

    assert connection.commits == 0
    assert connection.rollbacks == 1
