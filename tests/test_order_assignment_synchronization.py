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
    if "CURRENT_DATE" in sql:
        return [{"database_current_date": date(2026, 8, 3)}]
    if "FROM orders" in sql:
        return [{
            "case_no": "C-1",
            "service_days": Decimal("2"),
            "service_hours_per_day": Decimal("8"),
            "start_date": date(2026, 8, 3),
            "end_date": date(2026, 8, 4),
            "actual_start_date": date(2026, 8, 3),
            "actual_end_date": date(2026, 8, 4),
            "identity_status": "一般身分",
        }]
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
    assignment_select = next(
        sql
        for sql, _ in connection.cursor_obj.executed
        if "FROM case_staff_assignments" in sql
    )
    assert " kind," not in assignment_select
    assert "original_assignment_id" not in assignment_select
    assert "substitution_work_date" not in assignment_select


def test_preview_marks_empty_plan_as_requires_allocation(monkeypatch):
    connection = Connection(standard_responses)
    monkeypatch.setattr(sync, "get_connection", lambda: connection)

    result = sync.preview_order_assignment_sync("C-1", preview_order_change(), [])

    assert result["sync_status"] == "requires_allocation"
    assert result["blocking_reasons"] == [{"code": "assignment_plan_required"}]
    assert result["required_schedule_removals"] == []
    assert not any("FOR UPDATE" in sql.upper() for sql, _ in connection.cursor_obj.executed)
    assert not any("UPDATE clients" in sql for sql, _ in connection.cursor_obj.executed)
    assert not any("UPDATE orders" in sql for sql, _ in connection.cursor_obj.executed)


def test_preview_bootstraps_future_legacy_order_without_assignments(monkeypatch):
    def responses(sql, params):
        if "FROM case_staff_assignments" in sql:
            return []
        return standard_responses(sql, params)

    connection = Connection(responses)
    monkeypatch.setattr(sync, "get_connection", lambda: connection)
    plan = assignment_plan()
    plan[0]["assignment_id"] = None

    result = sync.preview_order_assignment_sync(
        "C-1",
        preview_order_change(),
        plan,
    )

    assert result["sync_status"] == "in_sync"
    assert result["blocking_reasons"] == []
    assert result["proposed_actual_hours"] == Decimal("16")


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
            return [{"id": 7, "assignment_id": 7, "staff_id": 11, "assignment_sequence": 1, "assigned_start_date": date(2026, 8, 3), "assigned_end_date": date(2026, 8, 4), "status": "planned", "planned_hours": Decimal("16"), "actual_hours": Decimal("16"), "service_hours": Decimal("16"), "hourly_rate": Decimal("400"), "floor_fee_amount": Decimal("0")}]
        if "FROM orders" in sql:
            row = standard_responses(sql, params)[0]
            return [{**row, "name": "舊客戶", "client_name": "舊客戶", "identity_status": "一般市民"}]
        if "FROM clients" in sql:
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
    changed_plan = assignment_plan()
    changed_plan[0]["assigned_end_date"] = "2026-08-03"

    result = sync.preview_order_assignment_sync("C-1", preview_order_change(), changed_plan)

    assert result["sync_status"] == "locked"
    assert {item["code"] for item in result["blocking_reasons"]} == {
        "active_staff_payment",
        "assignment_plan_invalid",
        "legacy_schedule_requires_review",
    }


def test_preview_does_not_lock_an_unchanged_assignment(monkeypatch):
    def responses(sql, params):
        if "FROM staff_payments" in sql:
            pytest.fail("unchanged assignment must not be checked as affected")
        return standard_responses(sql, params)

    connection = Connection(responses)
    monkeypatch.setattr(sync, "get_connection", lambda: connection)

    result = sync.preview_order_assignment_sync("C-1", preview_order_change(), assignment_plan())

    assert result["sync_status"] == "in_sync"
    assert result["blocking_reasons"] == []


def test_preview_order_hours_change_checks_every_active_assignment_lock(monkeypatch):
    def responses(sql, params):
        if "FROM orders" in sql:
            row = standard_responses(sql, params)[0]
            return [{**row, "service_hours_per_day": Decimal("6")}]
        if "FROM staff_payments" in sql:
            assert params == (7,)
            return [{"assignment_id": 7}]
        return standard_responses(sql, params)

    connection = Connection(responses)
    monkeypatch.setattr(sync, "get_connection", lambda: connection)

    result = sync.preview_order_assignment_sync("C-1", preview_order_change(), assignment_plan())

    assert result["sync_status"] == "locked"
    assert result["blocking_reasons"] == [
        {"code": "active_staff_payment", "assignment_id": 7}
    ]


@pytest.mark.parametrize(
    ("field", "stored_value"),
    [
        ("start_date", date(2026, 8, 2)),
        ("end_date", date(2026, 8, 5)),
        ("actual_start_date", date(2026, 8, 2)),
        ("actual_end_date", date(2026, 8, 5)),
    ],
)
@pytest.mark.parametrize(
    ("lock_table", "expected_code", "expected_status"),
    [
        (
            "FROM staff_monthly_settlement_details",
            "active_monthly_settlement",
            "locked",
        ),
        (
            "FROM actual_hours_adjustments",
            "manual_actual_hours_adjustment",
            "requires_review",
        ),
    ],
)
def test_preview_order_date_change_checks_every_active_assignment_lock(
    monkeypatch,
    field,
    stored_value,
    lock_table,
    expected_code,
    expected_status,
):
    def responses(sql, params):
        if "FROM orders" in sql:
            row = standard_responses(sql, params)[0]
            return [{**row, field: stored_value}]
        if lock_table in sql:
            assert params == (7,)
            return [{"assignment_id": 7}]
        return standard_responses(sql, params)

    connection = Connection(responses)
    monkeypatch.setattr(sync, "get_connection", lambda: connection)

    result = sync.preview_order_assignment_sync(
        "C-1",
        preview_order_change(),
        assignment_plan(),
    )

    assert result["sync_status"] == expected_status
    assert {"code": expected_code, "assignment_id": 7} in result["blocking_reasons"]


def test_preview_rejects_historical_assignment_and_does_not_offer_schedule_removal(monkeypatch):
    def responses(sql, params):
        if "CURRENT_DATE" in sql:
            return [{"database_current_date": date(2026, 8, 4)}]
        if "WHERE case_no = %s AND assignment_id IN" in sql:
            return [{
                "id": 100,
                "case_no": "C-1",
                "assignment_id": 7,
                "staff_id": 11,
                "work_date": date(2026, 8, 3),
            }]
        return standard_responses(sql, params)

    connection = Connection(responses)
    monkeypatch.setattr(sync, "get_connection", lambda: connection)
    changed_plan = assignment_plan()
    changed_plan[0]["assigned_start_date"] = "2026-08-04"

    result = sync.preview_order_assignment_sync("C-1", preview_order_change(), changed_plan)

    assert result["required_schedule_removals"] == []
    assert {reason["code"] for reason in result["blocking_reasons"]} == {
        "assignment_plan_invalid",
        "historical_schedule_immutable",
    }


def test_preview_rejects_more_than_four_segments_before_connecting(monkeypatch):
    monkeypatch.setattr(sync, "get_connection", lambda: pytest.fail("must not connect"))
    plan = [
        {
            "assignment_id": None,
            "staff_id": 10 + sequence,
            "assignment_sequence": sequence,
            "assigned_start_date": "2026-08-03",
            "assigned_end_date": "2026-08-03",
        }
        for sequence in range(1, 6)
    ]

    with pytest.raises(ValueError, match="more than four"):
        sync.preview_order_assignment_sync("C-1", preview_order_change(), plan)


def test_preview_lists_only_assignment_owned_rows_for_an_omitted_assignment(monkeypatch):
    def responses(sql, params):
        if "FROM case_staff_assignments" in sql:
            return [
                {"id": 7, "staff_id": 11, "assignment_sequence": 1, "assigned_start_date": date(2026, 8, 3), "assigned_end_date": date(2026, 8, 4), "status": "planned", "planned_hours": Decimal("16"), "actual_hours": Decimal("16")},
                {"id": 8, "staff_id": 12, "assignment_sequence": 2, "assigned_start_date": date(2026, 8, 5), "assigned_end_date": date(2026, 8, 5), "status": "planned", "planned_hours": Decimal("8"), "actual_hours": Decimal("8")},
            ]
        if "FROM staff_payments" in sql:
            assert params == (8,)
            return [{"assignment_id": 8}]
        if "WHERE case_no = %s AND assignment_id IN" in sql:
            assert params == ("C-1", 8)
            return [
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
    assert {item["code"] for item in result["blocking_reasons"]} == {
        "active_staff_payment",
        "assignment_plan_invalid",
    }
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
        if "FROM orders" in sql:
            row = standard_responses(sql, params)[0]
            return [{**row, "service_hours_per_day": Decimal("6")}]
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
        if "FROM orders" in sql:
            row = standard_responses(sql, params)[0]
            return [{
                **row,
                "service_days": Decimal("3"),
                "end_date": date(2026, 8, 5),
                "actual_end_date": date(2026, 8, 5),
            }]
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
    assignment_update = next(
        (sql, params)
        for sql, params in connection.cursor_obj.executed
        if "SET staff_id = %s, assignment_sequence = %s" in sql
    )
    assert "assigned_start_date = %s, assigned_end_date = %s" in assignment_update[0]
    assert assignment_update[1][:5] == (
        11,
        1,
        date(2026, 8, 3),
        date(2026, 8, 4),
        Decimal("16"),
    )
    business_writes = [
        statement
        for statement in statements
        if statement.startswith(("INSERT", "UPDATE", "DELETE"))
    ]
    assert business_writes[-1].startswith("INSERT INTO order_assignment_change_audits")


def test_apply_rejects_historical_schedule_removal_before_writes(monkeypatch):
    def responses(sql, params):
        if "CURRENT_DATE" in sql:
            return [{"database_current_date": date(2026, 8, 4)}]
        if "FROM orders" in sql:
            row = standard_responses(sql, params)[0]
            return [{
                **row,
                "service_days": Decimal("3"),
                "end_date": date(2026, 8, 5),
                "actual_end_date": date(2026, 8, 5),
            }]
        if "FROM case_staff_assignments" in sql:
            return [{
                "id": 7,
                "staff_id": 11,
                "assignment_sequence": 1,
                "assigned_start_date": date(2026, 8, 3),
                "assigned_end_date": date(2026, 8, 5),
                "status": "planned",
                "planned_hours": Decimal("24"),
                "actual_hours": Decimal("24"),
            }]
        if "WHERE case_no = %s AND assignment_id IN" in sql:
            return [{
                "id": 99,
                "case_no": "C-1",
                "assignment_id": 7,
                "staff_id": 11,
                "work_date": date(2026, 8, 3),
            }]
        return standard_responses(sql, params)

    connection = Connection(responses)
    monkeypatch.setattr(sync, "get_connection", lambda: connection)
    change = order_change()
    change.update({
        "start_date": "2026-08-04",
        "actual_start_date": "2026-08-04",
        "end_date": "2026-08-05",
        "actual_end_date": "2026-08-05",
    })
    plan = [{
        "assignment_id": None,
        "staff_id": 12,
        "assignment_sequence": 1,
        "assigned_start_date": "2026-08-04",
        "assigned_end_date": "2026-08-05",
    }]

    with pytest.raises(ValueError, match="exactly match"):
        sync.apply_order_assignment_sync(
            "C-1", change, plan, {"remove_schedule_ids": [99]}, "admin"
        )

    assert connection.commits == 0 and connection.rollbacks == 1
    assert not any(
        sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        for sql, _ in connection.cursor_obj.executed
    )


def test_apply_rolls_back_generation_failure_without_audit(monkeypatch):
    def responses(sql, params):
        if sql.startswith(("UPDATE", "DELETE", "INSERT")):
            return []
        if "FROM orders" in sql:
            row = standard_responses(sql, params)[0]
            return [{**row, "service_hours_per_day": Decimal("6")}]
        return standard_responses(sql, params)

    connection = Connection(responses)
    connection.cursor_obj.lastrowid = 501
    connection.cursor_obj.rowcount = 1
    monkeypatch.setattr(sync, "get_connection", lambda: connection)
    monkeypatch.setattr(
        sync,
        "generate_assignment_schedule_in_transaction",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("generation failed")),
    )

    with pytest.raises(RuntimeError, match="generation failed"):
        sync.apply_order_assignment_sync(
            "C-1", order_change(), assignment_plan(), {"remove_schedule_ids": []}, "admin"
        )

    assert connection.commits == 0 and connection.rollbacks == 1
    assert not any(
        "INSERT INTO order_assignment_change_audits" in sql
        for sql, _ in connection.cursor_obj.executed
    )
    assert connection.closed is True


def test_apply_rolls_back_when_decimal_actual_hours_do_not_match(monkeypatch):
    def responses(sql, params):
        if sql.startswith(("UPDATE", "DELETE", "INSERT")):
            return []
        if "FROM orders" in sql:
            row = standard_responses(sql, params)[0]
            return [{**row, "service_hours_per_day": Decimal("6")}]
        if "actual_hours" in sql and "status <> 'cancelled'" in sql:
            return [{"assignment_id": 7, "staff_id": 11, "actual_hours": Decimal("15.5")}]
        return standard_responses(sql, params)

    connection = Connection(responses)
    connection.cursor_obj.lastrowid = 501
    connection.cursor_obj.rowcount = 1
    monkeypatch.setattr(sync, "get_connection", lambda: connection)
    monkeypatch.setattr(
        sync,
        "generate_assignment_schedule_in_transaction",
        lambda _cursor, assignment_id: {
            "assignment_id": assignment_id,
            "assignment_schedule": [],
            "actual_hours": Decimal("15.5"),
        },
    )

    with pytest.raises(ValueError, match="actual-hours total"):
        sync.apply_order_assignment_sync(
            "C-1", order_change(), assignment_plan(), {"remove_schedule_ids": []}, "admin"
        )

    assert connection.commits == 0 and connection.rollbacks == 1


@pytest.mark.parametrize(
    ("lock_table", "code", "status"),
    [
        ("FROM staff_payments", "active_staff_payment", "locked"),
        ("FROM staff_monthly_settlement_details", "active_monthly_settlement", "locked"),
        ("FROM actual_hours_adjustments", "manual_actual_hours_adjustment", "requires_review"),
    ],
)
def test_apply_financial_and_manual_hours_locks_before_writes(
    monkeypatch, lock_table, code, status
):
    def responses(sql, params):
        if "FROM orders" in sql:
            row = standard_responses(sql, params)[0]
            return [{**row, "service_hours_per_day": Decimal("6")}]
        if lock_table in sql:
            return [{"assignment_id": 7}]
        return standard_responses(sql, params)

    connection = Connection(responses)
    monkeypatch.setattr(sync, "get_connection", lambda: connection)

    result = sync.apply_order_assignment_sync(
        "C-1", order_change(), assignment_plan(), {"remove_schedule_ids": []}, "admin"
    )

    assert result == {
        "case_no": "C-1",
        "sync_status": status,
        "blocking_reasons": [{"code": code, "assignment_id": 7}],
    }
    assert connection.commits == 0 and connection.rollbacks == 1 and connection.closed
    assert not any(
        sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        for sql, _ in connection.cursor_obj.executed
    )


def test_apply_legacy_schedule_blocks_without_removal(monkeypatch):
    def responses(sql, params):
        if "FROM orders" in sql:
            row = standard_responses(sql, params)[0]
            return [{**row, "service_hours_per_day": Decimal("6")}]
        if "assignment_id IS NULL" in sql:
            return [{"id": 91, "work_date": date(2026, 8, 3)}]
        return standard_responses(sql, params)

    connection = Connection(responses)
    monkeypatch.setattr(sync, "get_connection", lambda: connection)

    result = sync.apply_order_assignment_sync(
        "C-1", order_change(), assignment_plan(), {"remove_schedule_ids": []}, "admin"
    )

    assert result["sync_status"] == "requires_review"
    assert result["blocking_reasons"] == [
        {
            "code": "legacy_schedule_requires_review",
            "schedule_id": 91,
            "assignment_id": None,
        }
    ]
    assert connection.commits == 0 and connection.rollbacks == 1 and connection.closed


@pytest.mark.parametrize(("label", "remove_ids"), [("missing", []), ("extra", [99, 100])])
def test_apply_rejects_non_exact_schedule_removal_sets(monkeypatch, label, remove_ids):
    def responses(sql, params):
        if "FROM orders" in sql:
            row = standard_responses(sql, params)[0]
            return [{
                **row,
                "service_days": Decimal("3"),
                "end_date": date(2026, 8, 5),
                "actual_end_date": date(2026, 8, 5),
            }]
        if "FROM case_staff_assignments" in sql:
            return [{
                "id": 7,
                "staff_id": 11,
                "assignment_sequence": 1,
                "assigned_start_date": date(2026, 8, 3),
                "assigned_end_date": date(2026, 8, 5),
                "status": "planned",
                "planned_hours": Decimal("24"),
                "actual_hours": Decimal("24"),
            }]
        if "WHERE case_no = %s AND assignment_id IN" in sql:
            return [{
                "id": 99,
                "case_no": "C-1",
                "assignment_id": 7,
                "staff_id": 11,
                "work_date": date(2026, 8, 5),
            }]
        return standard_responses(sql, params)

    connection = Connection(responses)
    monkeypatch.setattr(sync, "get_connection", lambda: connection)

    with pytest.raises(ValueError, match="exactly match"):
        sync.apply_order_assignment_sync(
            "C-1", order_change(), assignment_plan(),
            {"remove_schedule_ids": remove_ids}, "admin",
        )

    assert label
    assert connection.commits == 0 and connection.rollbacks == 1 and connection.closed
    assert not any(
        sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        for sql, _ in connection.cursor_obj.executed
    )


@pytest.mark.parametrize(
    ("row", "remove_id", "expected_code"),
    [
        (
            {
                "id": 200,
                "case_no": "OTHER",
                "assignment_id": 7,
                "work_date": date(2026, 8, 3),
            },
            200,
            "schedule_case_mismatch",
        ),
        (
            {
                "id": 201,
                "case_no": "C-1",
                "assignment_id": 8,
                "work_date": date(2026, 8, 3),
            },
            201,
            "schedule_conflict",
        ),
    ],
)
def test_apply_rejects_cross_case_or_other_assignment_schedule_removal(
    monkeypatch, row, remove_id, expected_code
):
    def responses(sql, params):
        if "FROM staff_schedule" in sql and "WHERE id IN" in sql:
            return [row]
        return standard_responses(sql, params)

    connection = Connection(responses)
    monkeypatch.setattr(sync, "get_connection", lambda: connection)

    result = sync.apply_order_assignment_sync(
        "C-1",
        order_change(),
        assignment_plan(),
        {"remove_schedule_ids": [remove_id]},
        "admin",
    )

    assert {reason["code"] for reason in result["blocking_reasons"]} == {expected_code}
    assert connection.commits == 0 and connection.rollbacks == 1 and connection.closed
    assert not any(
        sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        for sql, _ in connection.cursor_obj.executed
    )


def test_apply_rejects_legacy_schedule_id_in_removal_plan(monkeypatch):
    def responses(sql, params):
        if "assignment_id IS NULL" in sql:
            return [{
                "id": 91,
                "case_no": "C-1",
                "assignment_id": None,
                "work_date": date(2026, 8, 3),
            }]
        return standard_responses(sql, params)

    connection = Connection(responses)
    monkeypatch.setattr(sync, "get_connection", lambda: connection)

    result = sync.apply_order_assignment_sync(
        "C-1",
        order_change(),
        assignment_plan(),
        {"remove_schedule_ids": [91]},
        "admin",
    )

    assert result["blocking_reasons"] == [{
        "code": "legacy_schedule_requires_review",
        "schedule_id": 91,
        "assignment_id": None,
    }]
    assert connection.commits == 0 and connection.rollbacks == 1 and connection.closed
    assert not any(
        sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        for sql, _ in connection.cursor_obj.executed
    )


@pytest.mark.parametrize(
    "failing_statement",
    [
        "UPDATE orders",
        "UPDATE clients",
        "UPDATE case_staff_assignments SET assignment_sequence",
        "UPDATE case_staff_assignments SET staff_id",
        "INSERT INTO order_assignment_change_audits",
    ],
)
def test_apply_each_core_write_failure_rolls_back_and_closes(
    monkeypatch, failing_statement
):
    def responses(sql, params):
        if failing_statement in sql:
            raise RuntimeError(f"failed: {failing_statement}")
        if sql.startswith(("UPDATE", "DELETE", "INSERT")):
            return []
        return standard_responses(sql, params)

    connection = Connection(responses)
    connection.cursor_obj.lastrowid = 501
    connection.cursor_obj.rowcount = 1
    monkeypatch.setattr(sync, "get_connection", lambda: connection)

    with pytest.raises(RuntimeError, match="failed:"):
        sync.apply_order_assignment_sync(
            "C-1", order_change(), assignment_plan(), {"remove_schedule_ids": []}, "admin"
        )

    assert connection.commits == 0 and connection.rollbacks == 1 and connection.closed
    audit_attempts = [
        sql for sql, _ in connection.cursor_obj.executed
        if "INSERT INTO order_assignment_change_audits" in sql
    ]
    if failing_statement == "INSERT INTO order_assignment_change_audits":
        assert len(audit_attempts) == 1
    else:
        assert audit_attempts == []


def test_apply_due_date_sync_failure_rolls_back_without_audit(monkeypatch):
    def responses(sql, params):
        if sql.startswith(("UPDATE", "DELETE", "INSERT")):
            return []
        return standard_responses(sql, params)

    connection = Connection(responses)
    connection.cursor_obj.lastrowid = 501
    connection.cursor_obj.rowcount = 1
    monkeypatch.setattr(sync, "get_connection", lambda: connection)
    monkeypatch.setattr(
        sync,
        "sync_client_payment_due_dates_for_case_no",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("due-date failed")),
    )

    with pytest.raises(RuntimeError, match="due-date failed"):
        sync.apply_order_assignment_sync(
            "C-1", order_change(), assignment_plan(), {"remove_schedule_ids": []}, "admin"
        )

    assert connection.commits == 0 and connection.rollbacks == 1 and connection.closed
    assert not any(
        "INSERT INTO order_assignment_change_audits" in sql
        for sql, _ in connection.cursor_obj.executed
    )


@pytest.mark.parametrize(
    ("activation_mode", "should_commit"),
    [
        ("review_required", True),
        ("unknown", False),
        ("raise", False),
    ],
)
def test_apply_subsidy_activation_outcomes_use_same_cursor(
    monkeypatch, activation_mode, should_commit
):
    activation_calls = []

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
        "load_case_accounting_source_with_cursor",
        lambda cursor, case_no: {
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
        lambda *_args: {
            "client_ledger_plan": {"subsidy_return_amount": Decimal("12000")}
        },
    )

    def activate(cursor, payment_id, amount, due_date):
        activation_calls.append((cursor, payment_id, amount, due_date))
        if activation_mode == "raise":
            raise RuntimeError("activation failed")
        return {"result": activation_mode}

    monkeypatch.setattr(sync, "activate_subsidy_return_obligation", activate)
    change = order_change()
    change["floor_fee"] = Decimal("0")

    if should_commit:
        result = sync.apply_order_assignment_sync(
            "C-1", change, assignment_plan(), {"remove_schedule_ids": []}, "admin"
        )
        assert result["subsidy_return_obligation"]["result"] == "review_required"
    else:
        expected = RuntimeError if activation_mode == "raise" else ValueError
        with pytest.raises(expected):
            sync.apply_order_assignment_sync(
                "C-1", change, assignment_plan(), {"remove_schedule_ids": []}, "admin"
            )

    assert len(activation_calls) == 1
    assert activation_calls[0][0] is connection.cursor_obj
    assert connection.closed is True
    assert connection.commits == (1 if should_commit else 0)
    assert connection.rollbacks == (0 if should_commit else 1)
    writes = [
        " ".join(sql.split())
        for sql, _ in connection.cursor_obj.executed
        if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
    ]
    if should_commit:
        assert writes[-1].startswith("INSERT INTO order_assignment_change_audits")
    else:
        assert not any(
            statement.startswith("INSERT INTO order_assignment_change_audits")
            for statement in writes
        )


def test_apply_replacement_cancels_old_assignment_and_creates_new_one(monkeypatch):
    def responses(sql, params):
        if sql.startswith(("UPDATE", "DELETE", "INSERT")):
            return []
        if "actual_hours" in sql and "status <> 'cancelled'" in sql:
            return [{"assignment_id": 8, "staff_id": 12, "actual_hours": Decimal("16")}]
        return standard_responses(sql, params)

    connection = Connection(responses)
    connection.cursor_obj.lastrowid = 8
    connection.cursor_obj.rowcount = 1
    monkeypatch.setattr(sync, "get_connection", lambda: connection)
    monkeypatch.setattr(
        sync,
        "generate_assignment_schedule_in_transaction",
        lambda _cursor, assignment_id: {
            "assignment_id": assignment_id,
            "assignment_schedule": [],
            "actual_hours": Decimal("16"),
        },
    )
    replacement = [{
        "assignment_id": None,
        "staff_id": 12,
        "assignment_sequence": 1,
        "assigned_start_date": "2026-08-03",
        "assigned_end_date": "2026-08-04",
    }]

    result = sync.apply_order_assignment_sync(
        "C-1", order_change(), replacement, {"remove_schedule_ids": []}, "admin"
    )

    assert result["assignments"][0]["assignment_id"] == 8
    assert any(
        "SET status = 'cancelled'" in sql and params == (7, "C-1")
        for sql, params in connection.cursor_obj.executed
    )
    assert any(
        "INSERT INTO case_staff_assignments" in sql
        for sql, _ in connection.cursor_obj.executed
    )
    assert connection.commits == 1 and connection.rollbacks == 0


@pytest.mark.parametrize(
    "failing_statement",
    [
        "INSERT INTO case_staff_assignments",
        "SET status = 'cancelled'",
    ],
)
def test_apply_create_or_cancel_failure_rolls_back_without_audit(
    monkeypatch, failing_statement
):
    def responses(sql, params):
        if failing_statement in sql:
            raise RuntimeError(f"failed: {failing_statement}")
        if sql.startswith(("UPDATE", "DELETE", "INSERT")):
            return []
        return standard_responses(sql, params)

    connection = Connection(responses)
    connection.cursor_obj.lastrowid = 8
    connection.cursor_obj.rowcount = 1
    monkeypatch.setattr(sync, "get_connection", lambda: connection)
    replacement = [{
        "assignment_id": None,
        "staff_id": 12,
        "assignment_sequence": 1,
        "assigned_start_date": "2026-08-03",
        "assigned_end_date": "2026-08-04",
    }]

    with pytest.raises(RuntimeError, match="failed:"):
        sync.apply_order_assignment_sync(
            "C-1", order_change(), replacement, {"remove_schedule_ids": []}, "admin"
        )

    assert connection.commits == 0 and connection.rollbacks == 1 and connection.closed
    assert not any(
        "INSERT INTO order_assignment_change_audits" in sql
        for sql, _ in connection.cursor_obj.executed
    )


def test_apply_schedule_delete_failure_rolls_back_without_audit(monkeypatch):
    def responses(sql, params):
        if sql.startswith("DELETE FROM staff_schedule"):
            raise RuntimeError("schedule delete failed")
        if sql.startswith(("UPDATE", "INSERT")):
            return []
        if "FROM orders" in sql:
            row = standard_responses(sql, params)[0]
            return [{
                **row,
                "service_days": Decimal("3"),
                "end_date": date(2026, 8, 5),
                "actual_end_date": date(2026, 8, 5),
            }]
        if "FROM case_staff_assignments" in sql:
            return [{
                "id": 7,
                "staff_id": 11,
                "assignment_sequence": 1,
                "assigned_start_date": date(2026, 8, 3),
                "assigned_end_date": date(2026, 8, 5),
                "status": "planned",
                "planned_hours": Decimal("24"),
                "actual_hours": Decimal("24"),
            }]
        if "WHERE case_no = %s AND assignment_id IN" in sql:
            return [{
                "id": 99,
                "case_no": "C-1",
                "assignment_id": 7,
                "staff_id": 11,
                "work_date": date(2026, 8, 5),
            }]
        return standard_responses(sql, params)

    connection = Connection(responses)
    connection.cursor_obj.rowcount = 1
    monkeypatch.setattr(sync, "get_connection", lambda: connection)

    with pytest.raises(RuntimeError, match="schedule delete failed"):
        sync.apply_order_assignment_sync(
            "C-1", order_change(), assignment_plan(),
            {"remove_schedule_ids": [99]}, "admin",
        )

    assert connection.commits == 0 and connection.rollbacks == 1 and connection.closed
    assert not any(
        "INSERT INTO order_assignment_change_audits" in sql
        for sql, _ in connection.cursor_obj.executed
    )
