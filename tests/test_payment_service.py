from decimal import Decimal

import pytest

from services import payment_service
from services.payment_service import calculate_staff_payable


def test_staff_payable_keeps_salary_and_floor_fee_separate():
    result = calculate_staff_payable(45, 350, 300, -50)
    assert result == {
        "service_hours": 45.0, "hourly_rate": 350.0, "service_salary": 15750.0,
        "floor_fee_amount": 300.0, "adjustment_amount": -50.0, "total_payable": 16000.0,
    }


def test_staff_payable_rejects_negative_total():
    with pytest.raises(ValueError, match="total payable"):
        calculate_staff_payable(1, 0, 0, -1)


class _Cursor:
    def __init__(self, assignment):
        self.assignment = assignment
        self.lastrowid = 73
        self.executions = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params):
        self.executions.append((sql, params))

    def fetchone(self):
        return self.assignment


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closes += 1


def test_create_staff_payment_uses_reconciled_assignment_owned_amounts(monkeypatch):
    cursor = _Cursor({"case_no": " CASE-25 "})
    connection = _Connection(cursor)
    reconciliation_calls = []

    monkeypatch.setattr(payment_service, "get_connection", lambda: connection)

    def reconcile(passed_cursor, case_no):
        reconciliation_calls.append((passed_cursor, case_no))
        return {
            "can_create_staff_payments": True,
            "assignments": [{
                "assignment_id": 25,
                "staff_id": 9,
                "actual_hours": Decimal("16"),
                "double_pay_hours": Decimal("8"),
                "hourly_rate": Decimal("350.00"),
                "floor_fee_allocated": Decimal("120.00"),
            }],
        }

    monkeypatch.setattr(
        payment_service, "reconcile_assignment_payroll_with_cursor", reconcile
    )

    assert payment_service.create_staff_payment(
        25, due_date="2026-08-01", adjustment_amount="-20"
    ) == 73
    assert reconciliation_calls == [(cursor, "CASE-25")]
    assert cursor.executions[-1][1] == (
        25, "CASE-25", 9, 24.0, 350.0, 8400.0, 120.0, -20.0, 8500.0,
        "2026-08-01",
    )
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.closes == 1


def test_create_staff_payment_fails_closed_when_reconciliation_rejects(monkeypatch):
    cursor = _Cursor({"case_no": "CASE-25"})
    connection = _Connection(cursor)
    monkeypatch.setattr(payment_service, "get_connection", lambda: connection)
    monkeypatch.setattr(
        payment_service,
        "reconcile_assignment_payroll_with_cursor",
        lambda passed_cursor, case_no: {
            "can_create_staff_payments": False,
            "assignments": [],
            "errors": [{"code": "payment_snapshot_mismatch"}],
        },
    )

    with pytest.raises(ValueError, match="reconciliation failed"):
        payment_service.create_staff_payment(25)

    assert len(cursor.executions) == 1
    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert connection.closes == 1
