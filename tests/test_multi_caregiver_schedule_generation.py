from datetime import date
from decimal import Decimal

import pytest

from services import multi_caregiver_schedule_generation as service


class FakeCursor:
    def __init__(self, responses):
        self.responses = list(responses)
        self.current = None
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
        if sql.lstrip().upper().startswith("SELECT"):
            self.current = self.responses.pop(0)

    def fetchone(self):
        return self.current

    def fetchall(self):
        return self.current


class FakeConnection:
    def __init__(self, responses):
        self.cursor_obj = FakeCursor(responses)
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class _TransactionMarker(Exception):
    """Marker exception for exception instance propagation assertions."""


def assignment(**overrides):
    return {
        "id": 21,
        "case_no": "115000001",
        "staff_id": 8,
        "status": "active",
        "assigned_start_date": date(2026, 6, 1),
        "assigned_end_date": date(2026, 6, 3),
        "service_hours_per_day": Decimal("9"),
        **overrides,
    }


def responses(
    *,
    target=None,
    assignments=None,
    payment=None,
    settlement=None,
    adjustments=None,
    rest='["Sunday"]',
    holidays=None,
    schedules=None,
):
    target = target or assignment()
    return [
        target,
        assignments if assignments is not None else [{k: target[k] for k in ("id", "status", "assigned_start_date", "assigned_end_date")}],
        payment,
        settlement,
        adjustments,
        {"weekly_rest_days": rest},
        holidays or [],
        schedules or [],
    ]


def test_generates_only_missing_assignment_rows_and_initial_actual_hours(monkeypatch):
    connection = FakeConnection(responses(holidays=[{"holiday_date": date(2026, 6, 2)}]))
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    result = service.generate_assignment_schedule(21)

    assert [row["work_date"] for row in result["assignment_schedule"]] == [
        date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)
    ]
    assert [row["is_work_day"] for row in result["assignment_schedule"]] == [True, False, True]
    assert result["actual_hours"] == Decimal("18")
    assert connection.commits == 1 and connection.rollbacks == 0 and connection.closed is True
    inserts = [item for item in connection.cursor_obj.executed if item[0].startswith("INSERT INTO staff_schedule")]
    assert len(inserts) == 3
    assert all("assignment_id" in item[0] for item in inserts)
    actual_hours_update = connection.cursor_obj.executed[-1]
    assert actual_hours_update[1] == (Decimal("18"), 21)


def test_transaction_generator_uses_callers_cursor_without_connection_boundary(monkeypatch):
    cursor = FakeCursor(responses(holidays=[{"holiday_date": date(2026, 6, 2)}]))
    monkeypatch.setattr(service, "get_connection", lambda: pytest.fail("must not open a connection"))

    result = service.generate_assignment_schedule_in_transaction(cursor, 21)

    assert result["actual_hours"] == Decimal("18")
    assert len(result["assignment_schedule"]) == 3
    assert any(sql.startswith("INSERT INTO staff_schedule") for sql, _ in cursor.executed)
    assert not any("COMMIT" in sql.upper() or "ROLLBACK" in sql.upper() for sql, _ in cursor.executed)


def test_transaction_rejects_actual_hours_adjustments_without_writes(monkeypatch):
    cursor = FakeCursor(responses(adjustments={"id": 3}))
    monkeypatch.setattr(service, "get_connection", lambda: pytest.fail("must not open a connection"))

    with pytest.raises(ValueError, match="actual-hours adjustments"):
        service.generate_assignment_schedule_in_transaction(cursor, 21)

    assert not any(sql.startswith("INSERT INTO staff_schedule") for sql, _ in cursor.executed)
    assert not any(sql.startswith("UPDATE case_staff_assignments") for sql, _ in cursor.executed)
    assert any(
        "actual_hours_adjustments" in sql and "FOR UPDATE" in sql.upper()
        for sql, _ in cursor.executed
    )


def test_transaction_rejects_payment_lock_without_writes(monkeypatch):
    cursor = FakeCursor(responses(payment={"id": 3}, adjustments=None))
    monkeypatch.setattr(service, "get_connection", lambda: pytest.fail("must not open a connection"))

    with pytest.raises(ValueError, match="non-cancelled staff payment"):
        service.generate_assignment_schedule_in_transaction(cursor, 21)

    assert not any(sql.startswith("INSERT INTO staff_schedule") for sql, _ in cursor.executed)
    assert not any(sql.startswith("UPDATE case_staff_assignments") for sql, _ in cursor.executed)
    assert any("staff_payments" in sql and "FOR UPDATE" in sql.upper() for sql, _ in cursor.executed)


def test_transaction_rejects_settlement_lock_without_writes(monkeypatch):
    cursor = FakeCursor(responses(settlement={"id": 4}, adjustments=None, payment=None))
    monkeypatch.setattr(service, "get_connection", lambda: pytest.fail("must not open a connection"))

    with pytest.raises(ValueError, match="active monthly settlement detail"):
        service.generate_assignment_schedule_in_transaction(cursor, 21)

    assert not any(sql.startswith("INSERT INTO staff_schedule") for sql, _ in cursor.executed)
    assert not any(sql.startswith("UPDATE case_staff_assignments") for sql, _ in cursor.executed)
    assert any("staff_monthly_settlement_details" in sql and "FOR UPDATE" in sql.upper() for sql, _ in cursor.executed)


def test_repeat_run_preserves_existing_manual_row_and_counts_it(monkeypatch):
    existing = {
        "id": 9, "case_no": "115000001", "staff_id": 8, "assignment_id": 21,
        "work_date": date(2026, 6, 1), "is_work_day": False,
        "is_double_pay": True, "notes": "人工調整",
    }
    connection = FakeConnection(responses(schedules=[existing]))
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    result = service.generate_assignment_schedule(21)

    assert [row["work_date"] for row in result["assignment_schedule"]] == [date(2026, 6, 2), date(2026, 6, 3)]
    assert result["actual_hours"] == Decimal("18")
    inserts = [item for item in connection.cursor_obj.executed if item[0].startswith("INSERT INTO staff_schedule")]
    assert all(params[3] != date(2026, 6, 1) for _, params in inserts)


@pytest.mark.parametrize("assignment_id", [0, "21", True])
def test_rejects_invalid_assignment_id_before_opening_connection(monkeypatch, assignment_id):
    monkeypatch.setattr(service, "get_connection", lambda: pytest.fail("must not connect"))

    with pytest.raises(ValueError, match="positive integer"):
        service.generate_assignment_schedule(assignment_id)


@pytest.mark.parametrize(
    ("schedules", "message"),
    [
        ([{"id": 9, "case_no": "115000001", "assignment_id": None, "work_date": date(2026, 6, 1), "is_work_day": True}], "staff already"),
        ([{"id": 9, "case_no": "115000002", "assignment_id": 21, "work_date": date(2026, 6, 1), "is_work_day": True}], "case mismatch"),
    ],
)
def test_rejects_legacy_or_wrong_case_schedule_conflicts(monkeypatch, schedules, message):
    connection = FakeConnection(responses(schedules=schedules))
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    with pytest.raises(ValueError, match=message):
        service.generate_assignment_schedule(21)

    assert connection.commits == 0 and connection.rollbacks == 1
    assert not any(sql.startswith("INSERT INTO staff_schedule") for sql, _ in connection.cursor_obj.executed)


@pytest.mark.parametrize(
    ("service_hours", "message"),
    [
        (None, "service_hours_per_day must be a finite positive decimal"),
        ("oops", "service_hours_per_day must be a finite positive decimal"),
        (Decimal("NaN"), "service_hours_per_day must be a finite positive decimal"),
        (Decimal("Infinity"), "service_hours_per_day must be a finite positive decimal"),
        (Decimal("-Infinity"), "service_hours_per_day must be a finite positive decimal"),
        (Decimal("0"), "service_hours_per_day must be a finite positive decimal"),
        (Decimal("-1"), "service_hours_per_day must be a finite positive decimal"),
    ],
)
def test_rejects_invalid_service_hours_before_writing_rows(monkeypatch, service_hours, message):
    connection = FakeConnection(
        responses(target=assignment(service_hours_per_day=service_hours))
    )
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    with pytest.raises(ValueError, match=message):
        service.generate_assignment_schedule(21)

    assert connection.commits == 0 and connection.rollbacks == 1
    assert not any(sql.startswith("INSERT INTO staff_schedule") for sql, _ in connection.cursor_obj.executed)
    assert not any(sql.startswith("UPDATE case_staff_assignments") for sql, _ in connection.cursor_obj.executed)


def test_rejects_assignment_locked_by_actual_hours_adjustments(monkeypatch):
    connection = FakeConnection(
        responses(settlement=None, adjustments={"id": 3})
    )
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    with pytest.raises(ValueError, match="actual-hours adjustments"):
        service.generate_assignment_schedule(21)

    assert connection.commits == 0 and connection.rollbacks == 1
    assert not any(sql.startswith("INSERT INTO staff_schedule") for sql, _ in connection.cursor_obj.executed)
    assert not any(sql.startswith("UPDATE case_staff_assignments") for sql, _ in connection.cursor_obj.executed)
    assert any(
        "actual_hours_adjustments" in sql and "FOR UPDATE" in sql.upper()
        for sql, _ in connection.cursor_obj.executed
    )


@pytest.mark.parametrize(
    "service_hours",
    [
        None,
        "oops",
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        Decimal("0"),
        Decimal("-1"),
    ],
)
def test_transaction_cursor_rejects_invalid_service_hours_without_writes(service_hours, monkeypatch):
    cursor = FakeCursor(responses(target=assignment(service_hours_per_day=service_hours)))
    monkeypatch.setattr(service, "get_connection", lambda: pytest.fail("must not open a connection"))

    with pytest.raises(ValueError, match="service_hours_per_day must be a finite positive decimal"):
        service.generate_assignment_schedule_in_transaction(cursor, 21)

    assert not any(sql.startswith("INSERT INTO staff_schedule") for sql, _ in cursor.executed)
    assert not any(sql.startswith("UPDATE case_staff_assignments") for sql, _ in cursor.executed)


def test_transaction_propagates_custom_exception_instance(monkeypatch):
    marker = _TransactionMarker("custom marker error")
    cursor = FakeCursor(responses())

    def raise_marker(_value: object) -> None:
        raise marker

    monkeypatch.setattr(service, "_normalize_service_hours", raise_marker)
    monkeypatch.setattr(service, "get_connection", lambda: pytest.fail("must not open a connection"))

    with pytest.raises(_TransactionMarker) as exc:
        service.generate_assignment_schedule_in_transaction(cursor, 21)

    assert exc.value is marker
    assert not any(sql.startswith("INSERT INTO staff_schedule") for sql, _ in cursor.executed)
    assert not any(sql.startswith("UPDATE case_staff_assignments") for sql, _ in cursor.executed)


@pytest.mark.parametrize(
    ("payment", "settlement", "message"),
    [({"id": 3}, None, "non-cancelled staff payment"), (None, {"id": 4}, "active monthly settlement detail")],
)
def test_rejects_assignment_locked_by_payment_or_settlement(monkeypatch, payment, settlement, message):
    connection = FakeConnection(responses(payment=payment, settlement=settlement))
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    with pytest.raises(ValueError, match=message):
        service.generate_assignment_schedule(21)

    assert connection.commits == 0 and connection.rollbacks == 1
