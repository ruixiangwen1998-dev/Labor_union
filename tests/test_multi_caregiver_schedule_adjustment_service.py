from datetime import date, timedelta
from decimal import Decimal

import pytest

from services import multi_caregiver_schedule_adjustment_service as subject

FIXED_DB_DATE = date(2026, 7, 10)


class Cursor:
    def __init__(self, responses):
        self.responses = list(responses)
        self.statements = []
        self._current = None

    def execute(self, sql, params=()):
        self.statements.append((sql, params))
        if "CURRENT_DATE" in sql.upper():
            if self.responses and isinstance(self.responses[0], dict) and (
                "current_date" in self.responses[0] or "CURRENT_DATE" in self.responses[0]
            ):
                self._current = self.responses.pop(0)
            else:
                self._current = {"current_date": date(2000, 1, 1)}
            return
        self._current = self.responses.pop(0) if self.responses else None

    def fetchone(self):
        return self._current

    def fetchall(self):
        return self._current or []


class Connection:
    def __init__(self, cursor):
        self.cursor_instance = cursor
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return _CursorContext(self.cursor_instance)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class _CursorContext:
    def __init__(self, cursor):
        self.cursor = cursor

    def __enter__(self):
        return self.cursor

    def __exit__(self, *args):
        return False


def _run(
    monkeypatch,
    responses,
    work_date="2026-07-02",
    is_work_day=False,
    is_double_pay=False,
    notes="請假調整",
):
    cursor = Cursor(responses)
    connection = Connection(cursor)
    monkeypatch.setattr(subject, "get_connection", lambda: connection)
    result = subject.adjust_assignment_schedule_day(11, work_date, is_work_day, is_double_pay, notes)
    return result, connection, cursor


def _assert_failed_update_block(connection: Connection, cursor: Cursor):
    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert not any("UPDATE staff_schedule" in sql for sql, _ in cursor.statements)
    assert not any(
        "UPDATE case_staff_assignments SET actual_hours" in sql for sql, _ in cursor.statements
    )


def _assignment(**overrides):
    return {
        "id": 11,
        "case_no": "CASE-1",
        "staff_id": 7,
        "assigned_start_date": date(2026, 7, 1),
        "assigned_end_date": date(2026, 7, 3),
        "status": "active",
        "service_days": 2,
        "service_hours_per_day": Decimal("9"),
    } | overrides


def _schedule(**overrides):
    return {
        "id": 21,
        "case_no": "CASE-1",
        "staff_id": 7,
        "assignment_id": 11,
        "work_date": date(2026, 7, 2),
        "is_work_day": True,
        "is_double_pay": False,
        "notes": None,
    } | overrides


def test_adjusts_only_target_assignment_and_recalculates_hours(monkeypatch):
    result, connection, cursor = _run(
        monkeypatch,
        [
            _assignment(), None, None, None, _schedule(), None,
            [{"id": 20, "is_work_day": True}, {"id": 21, "is_work_day": False}, {"id": 22, "is_work_day": True}],
            None, [{"assignment_id": 11, "actual_hours": Decimal("18")}],
        ],
    )

    assert result["actual_hours"] == Decimal("18")
    assert result["case_actual_hours"] == Decimal("18")
    assert result["order_planned_hours"] == Decimal("18")
    assert result["adjusted_schedule_day"]["assignment_id"] == 11
    assert result["adjusted_schedule_day"]["is_work_day"] is False
    assert connection.commits == 1
    assert connection.rollbacks == 0
    update_sql, update_params = next(
        (sql, params) for sql, params in cursor.statements if "UPDATE staff_schedule" in sql
    )
    assert "UPDATE staff_schedule" in update_sql
    assert update_params == (False, False, "請假調整", 21, 11)
    hours_sql, hours_params = next(
        (sql, params) for sql, params in cursor.statements if "UPDATE case_staff_assignments SET actual_hours" in sql
    )
    assert hours_sql.startswith("UPDATE case_staff_assignments SET actual_hours")
    assert hours_params == (Decimal("18"), 11)
    all_sql = "\n".join(sql for sql, _ in cursor.statements).upper()
    assert "INSERT" not in all_sql
    assert "DELETE" not in all_sql
    assert "ON DUPLICATE" not in all_sql
    assert "UPDATE ORDERS" not in all_sql
    assert all(
        "FOR UPDATE" in sql
        for sql, _ in cursor.statements
        if "SELECT" in sql.upper() and "CURRENT_DATE" not in sql.upper()
    )


@pytest.mark.parametrize(
    ("assignment", "schedule", "message"),
    [
        (_assignment(status="cancelled"), None, "cancelled assignment"),
        (_assignment(assigned_start_date=None), None, "date range is incomplete"),
        (_assignment(), _schedule(assignment_id=None), "requires review"),
        (_assignment(), _schedule(assignment_id=99), "another assignment"),
        (_assignment(), _schedule(case_no="CASE-OTHER"), "case does not match"),
    ],
)
def test_rejects_cancelled_incomplete_or_unowned_schedule(monkeypatch, assignment, schedule, message):
    responses = [assignment]
    if assignment["status"] != "cancelled" and assignment["assigned_start_date"] is not None:
        responses.extend([None, None, None, schedule])
    cursor = Cursor(responses)
    connection = Connection(cursor)
    monkeypatch.setattr(subject, "get_connection", lambda: connection)

    with pytest.raises(ValueError, match=message):
        subject.adjust_assignment_schedule_day(11, "2026-07-02", False, False, None)

    _assert_failed_update_block(connection, cursor)


def test_rejects_date_outside_assignment_or_missing_schedule(monkeypatch):
    cursor = Cursor([_assignment()])
    connection = Connection(cursor)
    monkeypatch.setattr(subject, "get_connection", lambda: connection)

    with pytest.raises(ValueError, match="outside"):
        subject.adjust_assignment_schedule_day(11, "2026-07-04", False, False, None)
    _assert_failed_update_block(connection, cursor)

    cursor = Cursor([_assignment(), None, None, None])
    connection = Connection(cursor)
    monkeypatch.setattr(subject, "get_connection", lambda: connection)
    with pytest.raises(ValueError, match="does not exist"):
        subject.adjust_assignment_schedule_day(11, "2026-07-02", False, False, None)
    _assert_failed_update_block(connection, cursor)


def test_rejects_historical_work_date(monkeypatch):
    yesterday = FIXED_DB_DATE - timedelta(days=1)
    cursor = Cursor(
        [
            {"current_date": FIXED_DB_DATE},
            None,
            None,
            None,
            _schedule(work_date=yesterday),
            None,
            [{"id": 20, "is_work_day": True}, {"id": 21, "is_work_day": True}],
            None,
        ]
    )
    connection = Connection(cursor)
    monkeypatch.setattr(subject, "get_connection", lambda: connection)

    with pytest.raises(ValueError, match="database current date"):
        subject.adjust_assignment_schedule_day(11, yesterday.isoformat(), False, False, None)

    _assert_failed_update_block(connection, cursor)


def test_allows_same_day_as_database_current_date_when_case_hours_stay_full(monkeypatch):
    cursor = Cursor(
        [
            {"current_date": FIXED_DB_DATE},
            _assignment(
                assigned_start_date=FIXED_DB_DATE,
                assigned_end_date=FIXED_DB_DATE,
                service_days=1,
            ),
            None,
            None,
            None,
            _schedule(work_date=FIXED_DB_DATE),
            None,
            [{"id": 20, "is_work_day": True}],
            None,
            [{"assignment_id": 11, "actual_hours": Decimal("9")}],
        ]
    )
    connection = Connection(cursor)
    monkeypatch.setattr(subject, "get_connection", lambda: connection)

    result = subject.adjust_assignment_schedule_day(
        11, FIXED_DB_DATE.isoformat(), True, True, "同日人工雙倍薪測試"
    )

    assert result["actual_hours"] == Decimal("9")
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_rolls_back_when_case_actual_hours_no_longer_fill_order_planned_hours(monkeypatch):
    cursor = Cursor(
        [
            _assignment(service_days=3),
            None,
            None,
            None,
            _schedule(),
            None,
            [{"id": 20, "is_work_day": True}, {"id": 21, "is_work_day": False}],
            None,
            [{"assignment_id": 11, "actual_hours": Decimal("18")}],
        ]
    )
    connection = Connection(cursor)
    monkeypatch.setattr(subject, "get_connection", lambda: connection)

    with pytest.raises(ValueError, match="order planned hours"):
        subject.adjust_assignment_schedule_day(
            11, "2026-07-02", False, False, "未同步補足服務日"
        )

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert any("UPDATE staff_schedule" in sql for sql, _ in cursor.statements)
    assert any(
        "UPDATE case_staff_assignments SET actual_hours" in sql
        for sql, _ in cursor.statements
    )


def test_allows_multi_caregiver_actual_hours_sum_that_fills_order_planned_hours(monkeypatch):
    result, connection, _cursor = _run(
        monkeypatch,
        [
            _assignment(service_days=3),
            None,
            None,
            None,
            _schedule(),
            None,
            [{"id": 20, "is_work_day": True}, {"id": 21, "is_work_day": False}, {"id": 22, "is_work_day": True}],
            None,
            [
                {"assignment_id": 11, "actual_hours": Decimal("18")},
                {"assignment_id": 12, "actual_hours": Decimal("9")},
            ],
        ],
    )

    assert result["actual_hours"] == Decimal("18")
    assert result["case_actual_hours"] == Decimal("27")
    assert result["order_planned_hours"] == Decimal("27")
    assert connection.commits == 1
    assert connection.rollbacks == 0


@pytest.mark.parametrize(
    ("payment", "settlement", "message"),
    [({"id": 1}, None, "active staff payment"), (None, {"id": 2}, "active monthly settlement")],
)
def test_rejects_payment_or_settlement_snapshot(monkeypatch, payment, settlement, message):
    cursor = Cursor([_assignment(), payment, settlement])
    connection = Connection(cursor)
    monkeypatch.setattr(subject, "get_connection", lambda: connection)

    with pytest.raises(ValueError, match=message):
        subject.adjust_assignment_schedule_day(11, "2026-07-02", False, False, None)

    _assert_failed_update_block(connection, cursor)


def test_rejects_double_pay_when_not_work_day(monkeypatch):
    monkeypatch.setattr(subject, "get_connection", lambda: pytest.fail("connection should not be opened"))

    with pytest.raises(ValueError, match="is_double_pay cannot be true"):
        subject.adjust_assignment_schedule_day(11, "2026-07-02", False, True, "休假無法列入雙倍薪")


@pytest.mark.parametrize(
    "service_hours_per_day",
    [
        None,
        "not-a-number",
        0,
        -9,
        float("nan"),
        float("inf"),
        -float("inf"),
        Decimal("0"),
        Decimal("-1"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
    ],
)
def test_rejects_invalid_service_hours_per_day(monkeypatch, service_hours_per_day):
    cursor = Cursor(
        [
            _assignment(service_hours_per_day=service_hours_per_day),
            None,
            None,
            None,
            _schedule(),
            None,
            [{"id": 20, "is_work_day": True}, {"id": 21, "is_work_day": False}],
            None,
        ]
    )
    connection = Connection(cursor)
    monkeypatch.setattr(subject, "get_connection", lambda: connection)

    with pytest.raises(ValueError, match="finite positive number"):
        subject.adjust_assignment_schedule_day(11, "2026-07-02", True, False, None)
    _assert_failed_update_block(connection, cursor)


def test_rejects_assignments_with_actual_hours_adjustments(monkeypatch):
    cursor = Cursor([_assignment(), None, None, {"id": 123}])
    connection = Connection(cursor)
    monkeypatch.setattr(subject, "get_connection", lambda: connection)

    with pytest.raises(ValueError, match="actual hours adjustments"):
        subject.adjust_assignment_schedule_day(11, "2026-07-02", False, False, None)

    _assert_failed_update_block(connection, cursor)
    assert any(
        "actual_hours_adjustments" in sql and "FOR UPDATE" in sql
        for sql, _ in cursor.statements
    )


def test_validates_request_without_opening_a_connection(monkeypatch):
    monkeypatch.setattr(subject, "get_connection", lambda: pytest.fail("connection should not be opened"))

    with pytest.raises(ValueError, match="positive integer"):
        subject.adjust_assignment_schedule_day(True, "2026-07-02", False, False, None)
    with pytest.raises(ValueError, match="boolean"):
        subject.adjust_assignment_schedule_day(11, "2026-07-02", 0, False, None)
    with pytest.raises(ValueError, match="ISO date"):
        subject.adjust_assignment_schedule_day(11, "not-a-date", False, False, None)
