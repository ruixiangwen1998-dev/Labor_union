from datetime import date
from decimal import Decimal

import pytest

from services import order_assignment_synchronization as sync


class Cursor:
    def __init__(self, responses):
        self.responses = responses
        self.executed = []
        self.rows = []

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        compact = " ".join(sql.split())
        self.rows = list(self.responses(compact, params))

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class Connection:
    def __init__(self, responses):
        self.cursor_obj = Cursor(responses)
        self.closed = False
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        class Context:
            def __enter__(inner):
                return self.cursor_obj

            def __exit__(inner, *_):
                return False

        return Context()

    def close(self):
        self.closed = True

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def order_change():
    return {
        "client_name": "王小明",
        "service_days": 2,
        "service_hours_per_day": 8,
        "floor_fee": 1200,
        "deposit_date": "2026-07-20",
        "start_date": "2026-08-03",
        "end_date": "2026-08-04",
        "actual_start_date": "2026-08-03",
        "actual_end_date": "2026-08-04",
    }


def preview_order_change():
    return order_change().copy()


def assignment_plan():
    return [
        {
            "assignment_id": 7,
            "staff_id": 11,
            "assignment_sequence": 1,
            "assigned_start_date": "2026-08-03",
            "assigned_end_date": "2026-08-04",
        }
    ]


def standard_responses(sql, _params):
    if "FROM orders" in sql:
        return [{"case_no": "C-1", "identity_status": "一般身分"}]
    if "FROM clients" in sql:
        return [{"name": "舊客戶"}]
    if "FROM case_staff_assignments" in sql:
        return [{"id": 7, "staff_id": 11, "assignment_sequence": 1, "assigned_start_date": date(2026, 8, 3), "assigned_end_date": date(2026, 8, 4), "status": "planned", "planned_hours": Decimal("16"), "actual_hours": Decimal("16")}]
    if "FROM staff_payments" in sql or "FROM staff_monthly_settlement_details" in sql or "FROM actual_hours_adjustments" in sql:
        return []
    if "FROM staff WHERE" in sql:
        return [{"weekly_rest_days": '["Sunday"]'}]
    if "FROM holidays" in sql or "FROM staff_schedule" in sql or "FROM client_payments" in sql:
        return []
    raise AssertionError(sql)


def test_preview_is_read_only_and_returns_exact_in_sync_result(monkeypatch):
    connection = Connection(standard_responses)
    monkeypatch.setattr(sync, "get_connection", lambda: connection)

    result = sync.preview_order_assignment_sync(" C-1 ", preview_order_change(), assignment_plan())

    assert result["sync_status"] == "in_sync"
    assert result["target_hours"] == Decimal("16")
    assert result["proposed_actual_hours"] == Decimal("16")
    assert result["difference"] == Decimal("0")
    assert result["blocking_reasons"] == []
    assert connection.closed is True
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")) for sql, _ in connection.cursor_obj.executed)


def test_preview_marks_empty_plan_as_requires_allocation(monkeypatch):
    connection = Connection(standard_responses)
    monkeypatch.setattr(sync, "get_connection", lambda: connection)

    result = sync.preview_order_assignment_sync("C-1", preview_order_change(), [])

    assert result["sync_status"] == "requires_allocation"
    assert result["blocking_reasons"] == [{"code": "assignment_plan_required"}]
    assert not any("FOR UPDATE" in sql.upper() for sql, _ in connection.cursor_obj.executed)
    assert not any("UPDATE clients" in sql for sql, _ in connection.cursor_obj.executed)
    assert not any("UPDATE orders" in sql for sql, _ in connection.cursor_obj.executed)


def test_apply_activates_subsidy_return_obligation_when_paid_and_completed(monkeypatch):
    called = []
    projection_calls = []

    def mock_responses(sql, params):
        if sql.startswith(("UPDATE", "DELETE", "INSERT")):
            return []
        if "FROM client_payments" in sql:
            return [{
                "id": 1,
                "case_no": "C-1",
                "amount_receivable": Decimal("10000"),
                "amount_received": Decimal("10000"),
                "subsidy_return_receivable": None,
                "deposit_due_date": "2026-07-01",
            }]
        if "FROM case_staff_assignments" in sql:
            return [{"id": 7, "assignment_id": 7, "staff_id": 11, "assignment_sequence": 1, "assigned_start_date": date(2026, 7, 1), "assigned_end_date": date(2026, 7, 31), "status": "planned", "planned_hours": Decimal("16"), "actual_hours": Decimal("16"), "service_hours": Decimal("16"), "hourly_rate": Decimal("400"), "floor_fee_amount": Decimal("0")}]
        if "FROM clients" in sql or "FROM orders" in sql:
            return [{"case_no": "C-1", "name": "舊客戶", "identity_status": "一般市民"}]
        return standard_responses(sql, params)

    connection = Connection(mock_responses)
    monkeypatch.setattr(sync, "get_connection", lambda: connection)
    monkeypatch.setattr(
        sync,
        "generate_assignment_schedule_in_transaction",
        lambda _cursor, assignment_id: {"assignment_schedule": [], "actual_hours": Decimal("16"), "assignment_id": assignment_id},
    )
    monkeypatch.setattr(
        sync,
        "load_case_accounting_source_with_cursor",
        lambda cursor, case_no: projection_calls.append((cursor, case_no)) or {
            "order": {
                "service_days": Decimal("2"),
                "service_hours_per_day": Decimal("8"),
                "floor_fee": Decimal("0"),
                "actual_start_date": date(2026, 8, 3),
                "actual_end_date": date(2026, 8, 4),
            },
            "client": {"identity_status": "一般市民"},
            "staff_assignments": [{
                "assignment_id": 7,
                "staff_id": 11,
                "actual_hours": Decimal("16"),
                "hourly_rate": Decimal("400"),
                "floor_fee_allocated": Decimal("0"),
                "status": "planned",
            }],
            "collection_schedule": {
                "deposit_service_days": Decimal("1"),
                "deposit_due_date": date(2026, 7, 1),
            },
            "missing_terms": [],
        },
    )
    monkeypatch.setattr(
        sync,
        "calculate_order_amounts",
        lambda order_terms, assignments, schedule: {
            "client_ledger_plan": {"subsidy_return_amount": Decimal("12000")}
        },
    )

    def activate(cursor, payment_id, amount, due_date):
        called.append((cursor, payment_id, amount, due_date))
        return {"result": "activated", "obligation": {"due_date": due_date}}

    monkeypatch.setattr(
        sync,
        "activate_subsidy_return_obligation",
        activate,
    )
    connection.cursor_obj.lastrowid = 501
    connection.cursor_obj.rowcount = 1

    change = order_change()
    change["floor_fee"] = Decimal("0")

    result = sync.apply_order_assignment_sync(
        "C-1", change, assignment_plan(), {"remove_schedule_ids": []}, "admin"
    )

    assert len(called) == 1
    assert projection_calls == [(connection.cursor_obj, "C-1")]
    assert called[0][0] is connection.cursor_obj
    assert called[0][1:] == (1, Decimal("12000"), "2026-09-05")
    assert result["subsidy_return_obligation"]["result"] == "activated"
    assert connection.commits == 1 and connection.rollbacks == 0


def test_apply_rolls_back_when_subsidy_return_projection_or_calculation_fails(monkeypatch):
    def responses(sql, params):
        if sql.startswith(("UPDATE", "DELETE", "INSERT")):
            return []
        if "FROM client_payments" in sql:
            return [{
                "id": 1,
                "amount_receivable": Decimal("10000"),
                "amount_received": Decimal("10000"),
                "subsidy_return_receivable": None,
            }]
        return standard_responses(sql, params)

    connection = Connection(responses)
    connection.cursor_obj.lastrowid = 501
    connection.cursor_obj.rowcount = 1
    monkeypatch.setattr(sync, "get_connection", lambda: connection)
    monkeypatch.setattr(
        sync,
        "generate_assignment_schedule_in_transaction",
        lambda _cursor, assignment_id: {
            "assignment_schedule": [],
            "actual_hours": Decimal("16"),
            "assignment_id": assignment_id,
        },
    )
    monkeypatch.setattr(
        sync,
        "load_case_accounting_source_with_cursor",
        lambda _cursor, _case_no: {
            "order": {
                "service_days": Decimal("2"),
                "service_hours_per_day": Decimal("8"),
                "floor_fee": Decimal("0"),
                "actual_start_date": date(2026, 8, 3),
                "actual_end_date": date(2026, 8, 4),
            },
            "client": {"identity_status": "一般市民"},
            "staff_assignments": [{
                "assignment_id": 7,
                "staff_id": 11,
                "actual_hours": Decimal("16"),
                "hourly_rate": Decimal("400"),
                "floor_fee_allocated": Decimal("0"),
                "status": "planned",
            }],
            "collection_schedule": {
                "deposit_service_days": Decimal("1"),
                "deposit_due_date": date(2026, 7, 1),
            },
            "missing_terms": [],
        },
    )

    def fail_calculation(*_args):
        raise RuntimeError("calculation failed")

    monkeypatch.setattr(sync, "calculate_order_amounts", fail_calculation)
    change = order_change()
    change["floor_fee"] = Decimal("0")

    with pytest.raises(RuntimeError, match="calculation failed"):
        sync.apply_order_assignment_sync(
            "C-1", change, assignment_plan(), {"remove_schedule_ids": []}, "admin"
        )

    assert connection.commits == 0 and connection.rollbacks == 1
    assert not any(
        "INSERT INTO order_assignment_change_audits" in sql
        for sql, _params in connection.cursor_obj.executed
    )


def test_preview_reports_payment_lock_and_legacy_schedule(monkeypatch):
    def responses(sql, params):
        if "FROM staff_payments" in sql:
            return [{"assignment_id": 7}]
        if "FROM staff_schedule" in sql:
            return [{"id": 99, "case_no": "C-1", "assignment_id": None, "work_date": date(2026, 8, 3)}]
        return standard_responses(sql, params)

    connection = Connection(responses)
    monkeypatch.setattr(sync, "get_connection", lambda: connection)

    result = sync.preview_order_assignment_sync("C-1", preview_order_change(), assignment_plan())

    assert result["sync_status"] == "locked"
    assert {item["code"] for item in result["blocking_reasons"]} == {"active_staff_payment", "legacy_schedule_requires_review"}


def test_preview_lists_only_assignment_owned_rows_for_an_omitted_assignment(monkeypatch):
    def responses(sql, params):
        if "FROM case_staff_assignments" in sql:
            return [
                {"id": 7, "staff_id": 11, "assignment_sequence": 1, "assigned_start_date": date(2026, 8, 3), "assigned_end_date": date(2026, 8, 4), "status": "planned", "planned_hours": Decimal("16"), "actual_hours": Decimal("16")},
                {"id": 8, "staff_id": 12, "assignment_sequence": 2, "assigned_start_date": date(2026, 8, 5), "assigned_end_date": date(2026, 8, 5), "status": "planned", "planned_hours": Decimal("8"), "actual_hours": Decimal("8")},
            ]
        if "FROM staff_payments" in sql:
            assert params == (7, 8)
            return [{"assignment_id": 8}]
        if "WHERE case_no = %s AND assignment_id IN" in sql:
            assert params == ("C-1", 7, 8)
            return [
                {"id": 100, "case_no": "C-1", "assignment_id": 7, "staff_id": 11, "work_date": date(2026, 8, 3)},
                {"id": 101, "case_no": "C-1", "assignment_id": 8, "staff_id": 12, "work_date": date(2026, 8, 5)},
            ]
        return standard_responses(sql, params)

    connection = Connection(responses)
    monkeypatch.setattr(sync, "get_connection", lambda: connection)

    result = sync.preview_order_assignment_sync("C-1", preview_order_change(), assignment_plan())

    assert result["sync_status"] == "locked"
    assert result["required_schedule_removals"] == [
        {"schedule_id": 101, "assignment_id": 8, "work_date": date(2026, 8, 5)}
    ]
    assert {item["code"] for item in result["blocking_reasons"]} == {"active_staff_payment"}
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")) for sql, _ in connection.cursor_obj.executed)


@pytest.mark.parametrize("field", ["actual_start_date", "actual_end_date"])
def test_preview_requires_complete_actual_order_dates(field):
    change = preview_order_change()
    change.pop(field)

    with pytest.raises(ValueError, match=field):
        sync.preview_order_assignment_sync("C-1", change, assignment_plan())


@pytest.mark.parametrize("field", ["clients.identity_status", "identity_status"])
def test_preview_rejects_client_supplied_identity_fields(monkeypatch, field):
    monkeypatch.setattr(sync, "get_connection", lambda: pytest.fail("must not connect"))
    change = preview_order_change()
    change[field] = "一般身分"

    with pytest.raises(ValueError, match="unsupported fields"):
        sync.preview_order_assignment_sync("C-1", change, assignment_plan())


def test_apply_requires_an_explicit_schedule_change_plan(monkeypatch):
    monkeypatch.setattr(sync, "get_connection", lambda: pytest.fail("must not connect"))

    result = sync.apply_order_assignment_sync("C-1", order_change(), assignment_plan(), {}, "admin")

    assert result == {
        "case_no": "C-1",
        "sync_status": "requires_allocation",
        "blocking_reasons": [{"code": "schedule_change_plan_required"}],
    }


@pytest.mark.parametrize("field", ["client_name", "floor_fee", "start_date", "actual_start_date"])
def test_apply_requires_complete_editable_order_target_before_connecting(monkeypatch, field):
    change = order_change()
    change.pop(field)
    monkeypatch.setattr(sync, "get_connection", lambda: pytest.fail("must not connect"))

    with pytest.raises(ValueError, match=field):
        sync.apply_order_assignment_sync(
            "C-1", change, assignment_plan(), {"remove_schedule_ids": []}, "admin"
        )


@pytest.mark.parametrize("field", ["clients.identity_status", "identity_status"])
def test_apply_rejects_client_supplied_identity_fields(monkeypatch, field):
    monkeypatch.setattr(sync, "get_connection", lambda: pytest.fail("must not connect"))
    change = order_change()
    change[field] = "一般身分"

    with pytest.raises(ValueError, match="unsupported fields"):
        sync.apply_order_assignment_sync(
            "C-1", change, assignment_plan(), {"remove_schedule_ids": []}, "admin"
        )


def test_apply_rejects_a_stale_or_extra_schedule_removal_before_writes(monkeypatch):
    connection = Connection(standard_responses)
    monkeypatch.setattr(sync, "get_connection", lambda: connection)

    with pytest.raises(ValueError, match="exactly match"):
        sync.apply_order_assignment_sync(
            "C-1", order_change(), assignment_plan(), {"remove_schedule_ids": [99]}, "admin"
        )

    assert connection.commits == 0 and connection.rollbacks == 1
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql, _ in connection.cursor_obj.executed)


def test_apply_returns_locked_without_any_business_write(monkeypatch):
    def responses(sql, params):
        if "FROM staff_payments" in sql:
            return [{"assignment_id": 7}]
        return standard_responses(sql, params)

    connection = Connection(responses)
    monkeypatch.setattr(sync, "get_connection", lambda: connection)

    result = sync.apply_order_assignment_sync(
        "C-1", order_change(), assignment_plan(), {"remove_schedule_ids": []}, "admin"
    )

    assert result == {
        "case_no": "C-1",
        "sync_status": "locked",
        "blocking_reasons": [{"code": "active_staff_payment", "assignment_id": 7}],
    }
    assert connection.commits == 0 and connection.rollbacks == 1
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql, _ in connection.cursor_obj.executed)


def test_apply_uses_one_transaction_and_audits_after_explicit_removal(monkeypatch):
    def responses(sql, params):
        if sql.startswith(("UPDATE", "DELETE", "INSERT")):
            return []
        if "FROM case_staff_assignments" in sql:
            if "actual_hours" in sql and "status <> 'cancelled'" in sql:
                return [{"assignment_id": 7, "staff_id": 11, "actual_hours": Decimal("16")}]
            return [{"id": 7, "staff_id": 11, "assignment_sequence": 1, "assigned_start_date": date(2026, 8, 3), "assigned_end_date": date(2026, 8, 5), "status": "planned", "planned_hours": Decimal("24"), "actual_hours": Decimal("24")}]
        if "WHERE case_no = %s AND assignment_id IN" in sql:
            return [{"id": 99, "case_no": "C-1", "assignment_id": 7, "staff_id": 11, "work_date": date(2026, 8, 5)}]
        return standard_responses(sql, params)

    connection = Connection(responses)
    monkeypatch.setattr(sync, "get_connection", lambda: connection)
    monkeypatch.setattr(
        sync,
        "generate_assignment_schedule_in_transaction",
        lambda _cursor, assignment_id: {"assignment_schedule": [], "actual_hours": Decimal("16"), "assignment_id": assignment_id},
    )
    connection.cursor_obj.lastrowid = 501
    connection.cursor_obj.rowcount = 1

    result = sync.apply_order_assignment_sync(
        "C-1", order_change(), assignment_plan(), {"remove_schedule_ids": [99]}, "admin"
    )

    assert result["audit_id"] == 501
    assert result["confirmation"]["can_confirm"] is True
    assert connection.commits == 1 and connection.rollbacks == 0 and connection.closed is True
    statements = [" ".join(sql.split()) for sql, _ in connection.cursor_obj.executed]
    assert any(statement.startswith("DELETE FROM staff_schedule") for statement in statements)
    order_update = next(params for sql, params in connection.cursor_obj.executed if "UPDATE orders" in sql)
    assert order_update[:5] == (
        Decimal("2"), Decimal("8"), Decimal("1200"), date(2026, 7, 20), date(2026, 8, 3)
    )
    order_update_sql = next(sql for sql, _ in connection.cursor_obj.executed if "UPDATE orders" in sql)
    assert "status = '訂單成立'" in order_update_sql
    assert any(
        "UPDATE clients SET name" in sql and params == ("王小明", "C-1")
        for sql, params in connection.cursor_obj.executed
    )
    assert any(statement.startswith("INSERT INTO order_assignment_change_audits") for statement in statements)
