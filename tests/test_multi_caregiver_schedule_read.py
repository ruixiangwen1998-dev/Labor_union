from datetime import date, timedelta
import json

import pytest

from services import multi_caregiver_schedule_read as service

FIXED_DB_DATE = date(2026, 7, 10)


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
        if self.responses:
            self.current = self.responses.pop(0)
        else:
            self.current = None

    def fetchone(self):
        return self.current

    def fetchall(self):
        return self.current


class FakeConnection:
    def __init__(self, responses):
        self.cursor_obj = FakeCursor(responses)
        self.closed = False
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class QueryAwareCursor:
    def __init__(self, fixture):
        self.fixture = fixture
        self.current = None
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), tuple(params) if params is not None else None))
        sql_upper = sql.upper()
        if "SELECT CURRENT_DATE AS CURRENT_DATE" in sql_upper:
            self.current = self.fixture.get("current_date")
        elif "FROM CASE_STAFF_ASSIGNMENTS A" in sql_upper and "WHERE A.ID = %S" in sql_upper:
            self.current = self.fixture["assignments"].get(params[0]) if params else None
        elif "FROM ACTUAL_HOURS_ADJUSTMENTS" in sql_upper:
            self.current = self.fixture["actual_hours_adjustments"].get(params[0]) if params else None
        elif "FROM STAFF_PAYMENTS" in sql_upper:
            self.current = self.fixture["staff_payments"].get(params[0]) if params else None
        elif "FROM STAFF_MONTHLY_SETTLEMENT_DETAILS" in sql_upper:
            self.current = self.fixture["staff_monthly_settlement_details"].get(params[0]) if params else None
        elif "FROM STAFF_SCHEDULE" in sql_upper:
            self.current = self.fixture["schedule_by_assignment"].get(params[0])
            if self.current is None and params:
                self.current = self.fixture["schedule_by_staff"].get(params[0], [])
        else:
            self.current = self.fixture.get("default", None)

    def fetchone(self):
        return self.current

    def fetchall(self):
        return self.current


class QueryAwareConnection:
    def __init__(self, fixture):
        self.cursor_obj = QueryAwareCursor(fixture)
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


def assignment(**overrides):
    return {
        "id": 21,
        "case_no": "115000001",
        "staff_id": 8,
        "staff_name": "王月嫂",
        "client_name": "陳客戶",
        "status": "active",
        "assigned_start_date": date(2026, 6, 1),
        "assigned_end_date": date(2026, 6, 3),
        "planned_hours": None,
        "actual_hours": 18,
        "service_hours_per_day": 9,
        **overrides,
    }


def schedule_day(**overrides):
    return {
        "id": 7,
        "case_no": "115000001",
        "staff_id": 8,
        "assignment_id": 21,
        "work_date": date(2026, 6, 1),
        "is_work_day": True,
        "is_double_pay": False,
        "notes": None,
        **overrides,
    }


def _assert_only_select_no_transaction(connection: FakeConnection):
    assert connection.commits == 0
    assert connection.rollbacks == 0
    assert all(stmt.upper().startswith("SELECT") for stmt, _ in connection.cursor_obj.executed)
    assert all("FOR UPDATE" not in stmt.upper() for stmt, _ in connection.cursor_obj.executed)


def test_reads_explicit_assignment_and_owned_schedule_days_with_fixed_db_date_and_adjustment_guard(monkeypatch):
    responses = [
        {"current_date": FIXED_DB_DATE},
        assignment(),
        None,
        None,
        None,
        [
            schedule_day(work_date=FIXED_DB_DATE - timedelta(days=1)),
            schedule_day(work_date=FIXED_DB_DATE, id=8),
            schedule_day(work_date=FIXED_DB_DATE + timedelta(days=1), id=9),
        ],
    ]
    connection = FakeConnection(responses)
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    result = service.get_assignment_schedule(21)

    assert result["assignment"]["id"] == 21
    assert result["assignment"]["staff_name"] == "王月嫂"
    assert result["database_current_date"] == FIXED_DB_DATE
    assert [row["work_date"] for row in result["schedule_days"]] == [
        FIXED_DB_DATE - timedelta(days=1),
        FIXED_DB_DATE,
        FIXED_DB_DATE + timedelta(days=1),
    ]
    assert [row["is_historical"] for row in result["schedule_days"]] == [True, False, False]
    assert result["adjustment_guard"] == {
        "is_cancelled": False,
        "has_actual_hours_adjustments": False,
        "has_active_staff_payment": False,
        "has_active_monthly_settlement": False,
        "reasons": [],
    }
    assert connection.cursor_obj.executed[1][0].startswith("SELECT a.id, a.case_no, a.staff_id")
    assert "WHERE assignment_id = %s" in connection.cursor_obj.executed[5][0]
    assert connection.closed is True
    _assert_only_select_no_transaction(connection)


@pytest.mark.parametrize("invalid_assignment_id", [0, "21", True, None])
def test_rejects_invalid_assignment_id_before_opening_connection(monkeypatch, invalid_assignment_id):
    monkeypatch.setattr(service, "get_connection", lambda: pytest.fail("must not connect"))
    with pytest.raises(ValueError, match="positive integer"):
        service.get_assignment_schedule(invalid_assignment_id)


def test_rejects_missing_assignment(monkeypatch):
    connection = FakeConnection([{"current_date": FIXED_DB_DATE}, None])
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    with pytest.raises(ValueError, match="does not exist"):
        service.get_assignment_schedule(21)

    assert len(connection.cursor_obj.executed) == 2
    assert connection.cursor_obj.executed[1][0].startswith("SELECT a.id, a.case_no, a.staff_id")
    assert connection.closed is True

def test_rejects_schedule_rows_not_owned_by_assignment_id(monkeypatch):
    connection = FakeConnection(
        [
            {"current_date": FIXED_DB_DATE},
            assignment(),
            None,
            None,
            None,
            [schedule_day(assignment_id=None)],
        ]
    )
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    with pytest.raises(ValueError, match="does not belong"):
        service.get_assignment_schedule(21)

    assert connection.closed is True


def test_rejects_schedule_rows_with_case_staff_mismatch(monkeypatch):
    connection = FakeConnection(
        [
            {"current_date": FIXED_DB_DATE},
            assignment(),
            None,
            None,
            None,
            [schedule_day(case_no="WRONG", staff_id=8)],
        ]
    )
    monkeypatch.setattr(service, "get_connection", lambda: connection)
    with pytest.raises(ValueError, match="case does not match"):
        service.get_assignment_schedule(21)

    assert connection.closed is True

    connection = FakeConnection(
        [
            {"current_date": FIXED_DB_DATE},
            assignment(),
            None,
            None,
            None,
            [schedule_day(case_no="115000001", staff_id=999)],
        ]
    )
    monkeypatch.setattr(service, "get_connection", lambda: connection)
    with pytest.raises(ValueError, match="staff does not match"):
        service.get_assignment_schedule(21)

    assert connection.closed is True


def test_adjustment_guard_evaluates_all_flags_and_keeps_fixed_order(monkeypatch):
    connection = FakeConnection(
        [
            {"current_date": FIXED_DB_DATE},
            assignment(status="cancelled"),
            {"id": 1},
            {"id": 2},
            {"id": 3},
            [schedule_day()],
        ]
    )
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    result = service.get_assignment_schedule(21)

    assert result["adjustment_guard"] == {
        "is_cancelled": True,
        "has_actual_hours_adjustments": True,
        "has_active_staff_payment": True,
        "has_active_monthly_settlement": True,
        "reasons": [
            "cancelled_assignment",
            "actual_hours_adjustment_exists",
            "active_staff_payment",
            "active_monthly_settlement",
        ],
    }
    # ensure no short-circuit in guard queries
    assert len(connection.cursor_obj.executed) == 6
    assert any("FROM actual_hours_adjustments" in sql for sql, _ in connection.cursor_obj.executed)
    assert any("FROM staff_payments" in sql for sql, _ in connection.cursor_obj.executed)
    assert any("FROM staff_monthly_settlement_details" in sql for sql, _ in connection.cursor_obj.executed)


def test_only_requested_assignment_rows_are_loaded(monkeypatch):
    same_staff_other_assignment_schedule = [
        schedule_day(),
        {"id": 33, "case_no": "115000002", "staff_id": 8, "assignment_id": 22, "work_date": date(2026, 6, 2)},
    ]
    connection = FakeConnection(
        [
            {"current_date": FIXED_DB_DATE},
            assignment(),
            None,
            None,
            None,
            same_staff_other_assignment_schedule,
        ]
    )
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    with pytest.raises(ValueError, match="does not belong"):
        service.get_assignment_schedule(21)

    assert connection.cursor_obj.executed[5][0].startswith("SELECT id, case_no, staff_id, assignment_id")
    assert connection.closed is True


def test_only_target_assignment_isolated_when_same_staff_has_multiple_assignments(monkeypatch):
    fixture = {
        "current_date": {"current_date": FIXED_DB_DATE},
        "assignments": {
            21: assignment(id=21, planned_hours=24, actual_hours=18, case_no="115000001", staff_id=8),
            22: assignment(
                id=22,
                planned_hours=80,
                actual_hours=99,
                case_no="115000002",
                staff_id=8,
            ),
        },
        "actual_hours_adjustments": {21: None, 22: None},
        "staff_payments": {21: None, 22: None},
        "staff_monthly_settlement_details": {21: None, 22: None},
        "schedule_by_assignment": {
            21: [
                schedule_day(id=7, case_no="115000001", staff_id=8, assignment_id=21, work_date=FIXED_DB_DATE - timedelta(days=1)),
                schedule_day(id=8, case_no="115000001", staff_id=8, assignment_id=21, work_date=FIXED_DB_DATE),
            ],
            22: [
                schedule_day(id=9, case_no="115000002", staff_id=8, assignment_id=22, work_date=FIXED_DB_DATE),
            ],
        },
        "schedule_by_staff": {
            8: [
                schedule_day(id=7, case_no="115000001", staff_id=8, assignment_id=21, work_date=FIXED_DB_DATE - timedelta(days=1)),
                schedule_day(id=8, case_no="115000001", staff_id=8, assignment_id=21, work_date=FIXED_DB_DATE),
                schedule_day(id=20, case_no="115000001", staff_id=8, assignment_id=None, work_date=FIXED_DB_DATE - timedelta(days=2)),
                schedule_day(id=9, case_no="115000002", staff_id=8, assignment_id=22, work_date=FIXED_DB_DATE),
            ],
        },
    }
    connection = QueryAwareConnection(fixture)
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    result = service.get_assignment_schedule(21)

    assert result["assignment"]["id"] == 21
    assert result["assignment"]["planned_hours"] == 24
    assert result["assignment"]["actual_hours"] == 18
    assert [row["assignment_id"] for row in result["schedule_days"]] == [21, 21]
    assert all(row["is_historical"] == (row["work_date"] < FIXED_DB_DATE) for row in result["schedule_days"])

    query_log = connection.cursor_obj.executed
    assert any("from case_staff_assignments a" in sql.lower() for sql, _ in query_log)
    assert any("from staff_schedule" in sql.lower() and params == (21,) for sql, params in query_log)
    assert not any("from staff_schedule" in sql.lower() and params == (8,) for sql, params in query_log)


def _conflict_snapshot_responses(**overrides):
    values = {
        "current_date": {"current_date": FIXED_DB_DATE},
        "order": {"case_no": "115000001"},
        "assignments": [
            {
                "id": 21,
                "case_no": "115000001",
                "staff_id": 8,
                "status": "active",
                "assigned_start_date": date(2026, 7, 1),
                "assigned_end_date": date(2026, 7, 31),
                "planned_hours": 180,
                "actual_hours": 90,
            }
        ],
        "schedules": [
            schedule_day(id=9, work_date=date(2026, 7, 12)),
            schedule_day(id=7, assignment_id=None, work_date=date(2026, 7, 11)),
        ],
        "locks": [
            {
                "id": 5,
                "lock_id": 4,
                "plan_id": 3,
                "case_no": "115000002",
                "segment_id": 2,
                "staff_id": 8,
                "lock_date": date(2026, 7, 13),
            }
        ],
        "events": [
            {
                "id": 6,
                "case_no": "115000001",
                "original_assignment_id": 21,
                "original_schedule_id": 9,
                "work_date": date(2026, 7, 12),
                "resolution_type": "leave_only",
                "substitute_assignment_id": None,
                "event_key": "event-6",
                "occurred_at": None,
            }
        ],
        "adjustments": [
            {
                "id": 10,
                "case_no": "115000001",
                "assignment_id": 21,
                "original_hours": 81,
                "adjusted_hours": 90,
                "reason": "correction",
                "adjusted_at": None,
            }
        ],
        "payments": [
            {
                "id": 11,
                "case_no": "115000001",
                "assignment_id": 21,
                "payment_status": "pending",
            }
        ],
        "settlements": [
            {
                "id": 12,
                "case_no": "115000001",
                "assignment_id": 21,
                "settlement_id": 13,
                "status": "draft",
            }
        ],
    }
    values.update(overrides)
    return [
        values["current_date"],
        values["order"],
        values["assignments"],
        values["schedules"],
        values["locks"],
        values["events"],
        values["adjustments"],
        values["payments"],
        values["settlements"],
        values.get("final_assignments", values["assignments"]),
    ]


def test_conflict_snapshot_returns_canonical_read_only_facts(monkeypatch):
    connection = FakeConnection(_conflict_snapshot_responses())
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    result = service.get_case_schedule_conflict_snapshot(
        " 115000001 ", [9, 8], "2026-07-01", "2026-07-31"
    )

    assert result["database_current_date"] == FIXED_DB_DATE
    assert [row["id"] for row in result["assignments"]] == [21]
    assert [row["id"] for row in result["assignment_schedule_days"]] == [7, 9]
    assert result["assignment_schedule_days"][0]["requires_review"] is True
    assert result["assignment_schedule_days"][1]["requires_review"] is False
    assert result["active_lock_days"][0]["lock_date"] == date(2026, 7, 13)
    assert result["historical_facts"]["leave_substitution_events"][0]["id"] == 6
    assert result["historical_facts"]["actual_hours_adjustments"][0]["id"] == 10
    assert result["historical_facts"]["non_cancelled_payments"][0]["id"] == 11
    assert result["historical_facts"]["active_settlements"][0]["id"] == 12
    assert len(connection.cursor_obj.executed) == 10
    assert connection.cursor_obj.executed[3][1] == (
        date(2026, 7, 1),
        date(2026, 7, 31),
        "115000001",
        8,
        9,
    )
    assert connection.cursor_obj.executed[4][1] == (8, 9, date(2026, 7, 1), date(2026, 7, 31))
    assert connection.closed is True
    _assert_only_select_no_transaction(connection)


def test_conflict_snapshot_case_not_found_is_immutable_json_safe_typed_error(monkeypatch):
    connection = FakeConnection(_conflict_snapshot_responses(order=None))
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    with pytest.raises(service.AssignmentScheduleConflictSnapshotDomainError) as raised:
        service.get_case_schedule_conflict_snapshot(
            " 115000001 ", [8], "2026-07-01", "2026-07-31"
        )

    error = raised.value
    assert error.category == "not_found"
    assert error.code == "case_not_found"
    assert error.as_dict() == {
        "category": "not_found",
        "code": "case_not_found",
        "details": {"case_no": "115000001"},
    }
    assert json.loads(json.dumps(error.as_dict())) == error.as_dict()
    error_details = error.details
    error_details["case_no"] = "changed"
    assert error.details == {"case_no": "115000001"}
    with pytest.raises(AttributeError):
        error.category = "conflict"
    assert connection.closed is True
    _assert_only_select_no_transaction(connection)


@pytest.mark.parametrize(
    "order_row",
    [
        {},
        [],
        0,
        True,
        {"case_no": 115000001},
        {"case_no": True},
        {"case_no": "WRONG"},
        {"wrong_key": "115000001"},
        {"case_no": None},
    ],
)
def test_conflict_snapshot_malformed_order_row_raises_value_error(monkeypatch, order_row):
    connection = FakeConnection(_conflict_snapshot_responses(order=order_row))
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    with pytest.raises(ValueError) as raised:
        service.get_case_schedule_conflict_snapshot(
            "115000001", [8], "2026-07-01", "2026-07-31"
        )

    assert not isinstance(raised.value, service.AssignmentScheduleConflictSnapshotDomainError)
    assert connection.closed is True
    _assert_only_select_no_transaction(connection)


@pytest.mark.parametrize(
    ("final_assignments", "expected_after"),
    [
        (
            [
                {"id": 31, "staff_id": 9},
                {"id": 21, "staff_id": 8},
            ],
            [
                {"assignment_id": 21, "staff_id": 8},
                {"assignment_id": 31, "staff_id": 9},
            ],
        ),
        ([], []),
        ([{"id": 21, "staff_id": 99}], [{"assignment_id": 21, "staff_id": 99}]),
    ],
    ids=["added", "removed", "staff_changed"],
)
def test_conflict_snapshot_detects_assignment_identity_drift(monkeypatch, final_assignments, expected_after):
    connection = FakeConnection(
        _conflict_snapshot_responses(final_assignments=final_assignments)
    )
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    with pytest.raises(service.AssignmentScheduleConflictSnapshotDomainError) as raised:
        service.get_case_schedule_conflict_snapshot(
            "115000001", [8], "2026-07-01", "2026-07-31"
        )

    assert raised.value.as_dict() == {
        "category": "conflict",
        "code": "assignment_identity_changed_during_snapshot",
        "details": {
            "case_no": "115000001",
            "before": [{"assignment_id": 21, "staff_id": 8}],
            "after": expected_after,
        },
    }
    assert connection.closed is True
    _assert_only_select_no_transaction(connection)


def test_conflict_snapshot_identity_comparison_is_stably_ordered(monkeypatch):
    assignments = _conflict_snapshot_responses()[2]
    assignments.append(
        {
            "id": 31,
            "case_no": "115000001",
            "staff_id": 9,
            "status": "active",
            "assigned_start_date": date(2026, 7, 1),
            "assigned_end_date": date(2026, 7, 31),
            "planned_hours": 180,
            "actual_hours": 90,
        }
    )
    connection = FakeConnection(
        _conflict_snapshot_responses(
            assignments=list(reversed(assignments)),
            final_assignments=[{"id": 31, "staff_id": 9}, {"id": 21, "staff_id": 8}],
        )
    )
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    result = service.get_case_schedule_conflict_snapshot(
        "115000001", [8, 9], "2026-07-01", "2026-07-31"
    )

    assert [row["id"] for row in result["assignments"]] == [21, 31]
    assert connection.closed is True
    _assert_only_select_no_transaction(connection)


@pytest.mark.parametrize(
    "responses",
    [
        _conflict_snapshot_responses(
            assignments=_conflict_snapshot_responses()[2] * 2,
        ),
        _conflict_snapshot_responses(final_assignments=[{"id": 21, "staff_id": 8}] * 2),
        _conflict_snapshot_responses(final_assignments=[{"id": "bad", "staff_id": 8}]),
    ],
    ids=["duplicate_initial", "duplicate_final", "malformed_final"],
)
def test_conflict_snapshot_duplicate_or_malformed_assignment_facts_propagate_value_error(monkeypatch, responses):
    connection = FakeConnection(responses)
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    with pytest.raises(ValueError) as raised:
        service.get_case_schedule_conflict_snapshot(
            "115000001", [8], "2026-07-01", "2026-07-31"
        )

    assert not isinstance(raised.value, service.AssignmentScheduleConflictSnapshotDomainError)
    assert connection.closed is True
    _assert_only_select_no_transaction(connection)


def test_conflict_snapshot_final_database_error_propagates_raw_and_closes_connection(monkeypatch):
    class FinalQueryFailureCursor(FakeCursor):
        def execute(self, sql, params=None):
            if len(self.executed) == 9:
                raise OSError("database unavailable")
            super().execute(sql, params)

    class FinalQueryFailureConnection(FakeConnection):
        def __init__(self, responses):
            super().__init__(responses)
            self.cursor_obj = FinalQueryFailureCursor(responses)

    connection = FinalQueryFailureConnection(_conflict_snapshot_responses())
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    with pytest.raises(OSError, match="database unavailable"):
        service.get_case_schedule_conflict_snapshot(
            "115000001", [8], "2026-07-01", "2026-07-31"
        )

    assert connection.closed is True
    _assert_only_select_no_transaction(connection)


@pytest.mark.parametrize(
    "args",
    [
        (" ", [8], "2026-07-01", "2026-07-31"),
        ("115000001", [0], "2026-07-01", "2026-07-31"),
        ("115000001", [-1], "2026-07-01", "2026-07-31"),
        ("115000001", [8.0], "2026-07-01", "2026-07-31"),
        ("115000001", ["9"], "2026-07-01", "2026-07-31"),
        ("115000001", [True], "2026-07-01", "2026-07-31"),
        ("", [8], "2026-07-01", "2026-07-31"),
        ("115000001", [8], "2026-07-32", "2026-07-31"),
        ("115000001", [8], "2026-08-01", "2026-07-31"),
    ],
)
def test_conflict_snapshot_rejects_invalid_input_before_connection(monkeypatch, args):
    monkeypatch.setattr(service, "get_connection", lambda: pytest.fail("must not connect"))
    with pytest.raises(ValueError):
        service.get_case_schedule_conflict_snapshot(*args)


def test_conflict_snapshot_empty_extra_staff_ids_merges_assignment_staff_and_keeps_locks(monkeypatch):
    connection = FakeConnection(_conflict_snapshot_responses())
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    result = service.get_case_schedule_conflict_snapshot(
        " 115000001 ",
        [],
        "2026-07-01",
        "2026-07-31",
    )

    assert result["assignment_schedule_days"][0]["id"] == 7
    assert result["active_lock_days"][0]["id"] == 5
    assert connection.cursor_obj.executed[3][1] == (
        date(2026, 7, 1),
        date(2026, 7, 31),
        "115000001",
        8,
    )
    assert connection.cursor_obj.executed[4][1] == (8, date(2026, 7, 1), date(2026, 7, 31))
    assert connection.closed is True
    _assert_only_select_no_transaction(connection)


def test_conflict_snapshot_empty_extra_staff_ids_and_no_assignments_queries_case_only_and_skips_locks(monkeypatch):
    responses = [
        {"current_date": FIXED_DB_DATE},
        {"case_no": "115000001"},
        [],
        [schedule_day(assignment_id=None, id=7, work_date=date(2026, 7, 12), is_work_day=False)],
        [],
        [],
        [],
        [],
        [],
    ]
    connection = FakeConnection(responses)
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    result = service.get_case_schedule_conflict_snapshot(
        "115000001",
        [],
        "2026-07-01",
        "2026-07-31",
    )

    assert result["active_lock_days"] == []
    assert connection.cursor_obj.executed[3][1] == (
        date(2026, 7, 1),
        date(2026, 7, 31),
        "115000001",
    )
    assert not any("caregiver_availability_lock_days" in sql.lower() for sql, _ in connection.cursor_obj.executed)
    assert connection.closed is True
    _assert_only_select_no_transaction(connection)


def test_conflict_snapshot_extra_staff_ids_is_canonicalized_and_deduplicated(monkeypatch):
    connection = FakeConnection(_conflict_snapshot_responses())
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    result = service.get_case_schedule_conflict_snapshot(
        "115000001",
        [9, 8, 9, 8],
        "2026-07-01",
        "2026-07-31",
    )

    assert [row["id"] for row in result["assignments"]] == [21]
    assert connection.cursor_obj.executed[3][1] == (
        date(2026, 7, 1),
        date(2026, 7, 31),
        "115000001",
        8,
        9,
    )
    assert connection.closed is True
    _assert_only_select_no_transaction(connection)


def test_conflict_snapshot_fails_closed_on_invalid_ownership_row(monkeypatch):
    invalid_schedule = schedule_day(
        id=30,
        case_no="115000001",
        assignment_id=999,
        work_date=date(2026, 7, 12),
    )
    connection = FakeConnection(_conflict_snapshot_responses(schedules=[invalid_schedule]))
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    with pytest.raises(ValueError, match="ownership mismatch"):
        service.get_case_schedule_conflict_snapshot(
            "115000001", [8], "2026-07-01", "2026-07-31"
        )

    assert connection.closed is True
    _assert_only_select_no_transaction(connection)


def test_conflict_snapshot_cursor_helper_keeps_snapshot_equal_and_only_adds_row_locks():
    read_cursor = FakeCursor(_conflict_snapshot_responses())
    locked_cursor = FakeCursor(_conflict_snapshot_responses())

    read_snapshot = service.get_case_schedule_conflict_snapshot_with_cursor(
        read_cursor,
        "115000001",
        [9, 8],
        "2026-07-01",
        "2026-07-31",
        False,
    )
    locked_snapshot = service.get_case_schedule_conflict_snapshot_with_cursor(
        locked_cursor,
        "115000001",
        [9, 8],
        "2026-07-01",
        "2026-07-31",
        True,
    )

    assert locked_snapshot == read_snapshot
    assert len(read_cursor.executed) == len(locked_cursor.executed) == 9
    assert all("FOR UPDATE" not in sql.upper() for sql, _ in read_cursor.executed)
    assert "FOR UPDATE" not in locked_cursor.executed[0][0].upper()
    assert all("FOR UPDATE" in sql.upper() for sql, _ in locked_cursor.executed[1:])
    assert [params for _, params in locked_cursor.executed] == [
        params for _, params in read_cursor.executed
    ]


@pytest.mark.parametrize("lock_rows", [None, 0, 1, "false"])
def test_conflict_snapshot_cursor_helper_rejects_non_bool_lock_rows(lock_rows):
    cursor = FakeCursor(_conflict_snapshot_responses())

    with pytest.raises(ValueError, match="lock_rows must be a bool"):
        service.get_case_schedule_conflict_snapshot_with_cursor(
            cursor,
            "115000001",
            [8],
            "2026-07-01",
            "2026-07-31",
            lock_rows,
        )

    assert cursor.executed == []


def test_conflict_snapshot_cursor_helper_canonicalizes_extra_staff_ids_and_unions_assignment_staff():
    cursor = FakeCursor(_conflict_snapshot_responses())

    result = service.get_case_schedule_conflict_snapshot_with_cursor(
        cursor,
        case_no="115000001",
        extra_staff_ids=[9, 8, 9, 8],
        range_start="2026-07-01",
        range_end="2026-07-31",
        lock_rows=False,
    )

    assert result["assignment_schedule_days"][0]["id"] == 7
    assert result["active_lock_days"][0]["id"] == 5
    assert cursor.executed[3][1] == (
        date(2026, 7, 1),
        date(2026, 7, 31),
        "115000001",
        8,
        9,
    )
    assert cursor.executed[4][1] == (8, 9, date(2026, 7, 1), date(2026, 7, 31))


def test_conflict_snapshot_cursor_helper_without_assignments_queries_case_only_and_skips_locks():
    responses = [
        {"current_date": FIXED_DB_DATE},
        {"case_no": "115000001"},
        [],
        [schedule_day(assignment_id=None, id=7, work_date=date(2026, 7, 12), is_work_day=False)],
        [],
        [],
        [],
        [],
    ]
    cursor = FakeCursor(responses)

    result = service.get_case_schedule_conflict_snapshot_with_cursor(
        cursor,
        case_no="115000001",
        extra_staff_ids=[],
        range_start="2026-07-01",
        range_end="2026-07-31",
        lock_rows=False,
    )

    assert len(result["active_lock_days"]) == 0
    assert cursor.executed[3][1] == (
        date(2026, 7, 1),
        date(2026, 7, 31),
        "115000001",
    )
    assert not any("caregiver_availability_lock_days" in sql.lower() for sql, _ in cursor.executed)


def test_conflict_snapshot_cursor_helper_rejects_invalid_current_date_row():
    cursor = FakeCursor([1])

    with pytest.raises(ValueError, match="unable to load database current date"):
        service.get_case_schedule_conflict_snapshot_with_cursor(
            cursor,
            case_no="115000001",
            extra_staff_ids=[8],
            range_start="2026-07-01",
            range_end="2026-07-31",
            lock_rows=False,
        )
