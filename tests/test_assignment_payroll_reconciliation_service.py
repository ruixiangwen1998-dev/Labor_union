from datetime import date
from decimal import Decimal

import pytest

from services.assignment_payroll_reconciliation_service import reconcile_assignment_payroll_with_cursor


class Cursor:
    def __init__(self, order, *rows):
        self.order, self.rows, self.current, self.statements = order, list(rows), None, []

    def execute(self, sql, params=()):
        self.statements.append((sql, params))
        self.current = self.order if "FROM orders" in sql else self.rows.pop(0)

    def fetchone(self):
        return self.current

    def fetchall(self):
        return self.current


def _order(days=2, hours=8, floor="100"):
    return {
        "case_no": "CASE-1",
        "service_days": Decimal(str(days)),
        "service_hours_per_day": Decimal(str(hours)),
        "floor_fee": Decimal(str(floor)),
    }


def _assignment(assignment_id=1, **overrides):
    return {
        "id": assignment_id,
        "case_no": "CASE-1",
        "staff_id": 7,
        "status": "active",
        "actual_hours": Decimal("16"),
        "hourly_rate": Decimal("100"),
        "floor_fee_allocated": Decimal("100"),
    } | overrides


def _schedule(day, assignment_id=1, **overrides):
    return {
        "id": (assignment_id or 0) * 100 + day.day,
        "case_no": "CASE-1",
        "staff_id": 7,
        "assignment_id": assignment_id,
        "work_date": day,
        "is_work_day": True,
        "is_double_pay": False,
    } | overrides


def _event(original_schedule_id, work_date, substitute_id=2, **overrides):
    return {
        "case_no": "CASE-1",
        "original_assignment_id": 1,
        "original_schedule_id": original_schedule_id,
        "work_date": work_date,
        "resolution_type": "substitute",
        "substitute_assignment_id": substitute_id,
        "prefix_assignment_id": None,
        "suffix_assignment_id": None,
    } | overrides


def _run(
    order,
    assignments,
    schedules,
    reviews=None,
    events=None,
    payments=None,
    settlements=None,
    pending_substitution_event=None,
):
    cursor = Cursor(
        order,
        assignments,
        schedules,
        reviews or [],
        events or [],
        payments or [],
        settlements or [],
    )
    return cursor, reconcile_assignment_payroll_with_cursor(
        cursor,
        " CASE-1 ",
        pending_substitution_event=pending_substitution_event,
    )


@pytest.mark.parametrize("count", [1, 2, 4])
def test_reconciles_one_two_and_four_assignment_owned_segments(count):
    assignments, schedules = [], []
    for assignment_id in range(1, count + 1):
        assignments.append(
            _assignment(
                assignment_id,
                staff_id=assignment_id,
                actual_hours=Decimal("8"),
                floor_fee_allocated=Decimal("25"),
            )
        )
        schedules.append(
            _schedule(
                date(2026, 7, assignment_id),
                assignment_id,
                staff_id=assignment_id,
            )
        )
    cursor, result = _run(_order(count, floor=str(25 * count)), assignments, schedules)
    assert result["can_create_staff_payments"] is True
    assert len(result["assignments"]) == count
    assert result["actual_hours_total"] == Decimal(8 * count)
    sql = "\n".join(statement for statement, _ in cursor.statements).upper()
    assert "INSERT INTO" not in sql and "UPDATE " not in sql and "DELETE FROM" not in sql
    assert all("FOR UPDATE" in statement.upper() for statement, _ in cursor.statements)


def test_same_staff_non_contiguous_assignments_remain_separate():
    assignments = [
        _assignment(1, actual_hours=Decimal("8"), floor_fee_allocated=Decimal("50")),
        _assignment(2, actual_hours=Decimal("8"), floor_fee_allocated=Decimal("50")),
    ]
    schedules = [_schedule(date(2026, 7, 1), 1), _schedule(date(2026, 7, 3), 2)]
    _, result = _run(_order(), assignments, schedules)
    assert result["can_create_staff_payments"] is True
    assert [row["assignment_id"] for row in result["assignments"]] == [1, 2]


def test_substitute_requires_exact_original_schedule_ownership_and_rate():
    leave_day = date(2026, 7, 2)
    original_schedule = _schedule(leave_day, 1, id=90, is_work_day=False)
    substitute_schedule = _schedule(leave_day, 2, id=91, staff_id=8)
    assignments = [
        _assignment(1, actual_hours=Decimal("0"), floor_fee_allocated=Decimal("0")),
        _assignment(
            2,
            staff_id=8,
            actual_hours=Decimal("8"),
            floor_fee_allocated=Decimal("100"),
        ),
    ]
    event = _event(90, leave_day)
    _, result = _run(_order(1), assignments, [original_schedule, substitute_schedule], events=[event])
    assert result["can_create_staff_payments"] is True
    assert result["assignments"][1]["service_salary"] == Decimal("800.00")

    for broken in (
        event | {"original_schedule_id": 999},
        event | {"original_schedule_id": 91},
        event | {"work_date": date(2026, 7, 3)},
    ):
        _, rejected = _run(
            _order(1), assignments, [original_schedule, substitute_schedule], events=[broken]
        )
        assert rejected["can_create_staff_payments"] is False
        assert "original_schedule_ownership_mismatch" in {
            error["code"] for error in rejected["errors"]
        }

    wrong_rate = [assignments[0], assignments[1] | {"hourly_rate": Decimal("101")}]
    _, rejected = _run(
        _order(1), wrong_rate, [original_schedule, substitute_schedule], events=[event]
    )
    assert "substitute_hourly_rate_mismatch" in {error["code"] for error in rejected["errors"]}


def test_substitute_double_pay_is_owned_only_by_substitute_assignment():
    day = date(2026, 7, 2)
    assignments = [
        _assignment(1, actual_hours=Decimal("0"), floor_fee_allocated=Decimal("0")),
        _assignment(2, staff_id=8, actual_hours=Decimal("8"), floor_fee_allocated=Decimal("100")),
    ]
    schedules = [
        _schedule(day, 1, id=90, is_work_day=False),
        _schedule(day, 2, id=91, staff_id=8, is_double_pay=True),
    ]
    _, result = _run(_order(1), assignments, schedules, events=[_event(90, day)])
    assert result["can_create_staff_payments"] is True
    assert result["assignments"][0]["double_pay_hours"] == Decimal("0")
    assert result["assignments"][1]["service_salary"] == Decimal("1600.00")


def test_family_floor_fee_rounding_leaves_one_cent_remainder_on_original():
    leave_day = date(2026, 7, 3)
    assignments = [
        _assignment(1, actual_hours=Decimal("16"), floor_fee_allocated=Decimal("0.01")),
        _assignment(
            2,
            staff_id=8,
            actual_hours=Decimal("8"),
            floor_fee_allocated=Decimal("0.00"),
        ),
    ]
    schedules = [
        _schedule(date(2026, 7, 1), 1),
        _schedule(date(2026, 7, 2), 1),
        _schedule(leave_day, 1, id=90, is_work_day=False),
        _schedule(leave_day, 2, id=91, staff_id=8),
    ]
    _, result = _run(
        _order(3, floor="0.01"), assignments, schedules, events=[_event(90, leave_day)]
    )
    assert result["can_create_staff_payments"] is True
    assert result["assignments"][0]["expected_family_floor_fee"] == Decimal("0.01")
    assert result["assignments"][1]["expected_family_floor_fee"] == Decimal("0.00")
    assert result["floor_fee_total"] == Decimal("0.01")


def test_money_is_quantized_round_half_up_to_cents():
    assignment = _assignment(
        actual_hours=Decimal("8"),
        hourly_rate=Decimal("1.005"),
        floor_fee_allocated=Decimal("0.005"),
    )
    _, result = _run(
        _order(1, floor="0.005"), [assignment], [_schedule(date(2026, 7, 1))]
    )
    detail = result["assignments"][0]
    assert result["can_create_staff_payments"] is True
    assert detail["hourly_rate"] == Decimal("1.01")
    assert detail["floor_fee_allocated"] == Decimal("0.01")
    assert detail["service_salary"] == Decimal("8.08")


@pytest.mark.parametrize("bad_value", [None, float("nan"), Decimal("NaN"), Decimal("-1")])
def test_missing_float_nonfinite_and_negative_amounts_fail_closed(bad_value):
    _, result = _run(
        _order(),
        [_assignment(actual_hours=bad_value)],
        [_schedule(date(2026, 7, 1)), _schedule(date(2026, 7, 2))],
    )
    assert result["can_create_staff_payments"] is False
    assert "assignment_amount_invalid" in {error["code"] for error in result["errors"]}


def test_legacy_review_cross_case_and_duplicate_rows_block_payment():
    schedules = [
        _schedule(date(2026, 7, 1), id=10),
        _schedule(date(2026, 7, 1), id=11),
        _schedule(date(2026, 7, 2), id=12, assignment_id=None),
        _schedule(date(2026, 7, 3), id=13, case_no="OTHER"),
    ]
    reviews = [{"schedule_id": 10, "review_status": "review_required", "resolved_assignment_id": None}]
    _, result = _run(_order(), [_assignment()], schedules, reviews=reviews)
    codes = {error["code"] for error in result["errors"]}
    assert {
        "duplicate_assignment_date",
        "legacy_schedule_requires_review",
        "schedule_ownership_mismatch",
        "schedule_review_required",
    } <= codes
    assert result["can_create_staff_payments"] is False


def test_payment_and_monthly_settlement_snapshots_match_or_fail_closed():
    payment = {
        "assignment_id": 1,
        "case_no": "CASE-1",
        "staff_id": 7,
        "service_hours": Decimal("16"),
        "hourly_rate": Decimal("100"),
        "service_salary": Decimal("1600"),
        "floor_fee_amount": Decimal("100"),
        "payment_status": "pending",
    }
    schedules = [_schedule(date(2026, 7, 1)), _schedule(date(2026, 7, 2))]
    _, clean = _run(_order(), [_assignment()], schedules, payments=[payment], settlements=[payment])
    assert clean["can_create_staff_payments"] is True

    for key, value in (
        ("case_no", "OTHER"),
        ("staff_id", 99),
        ("service_hours", Decimal("15")),
        ("hourly_rate", Decimal("99")),
        ("service_salary", Decimal("1599")),
        ("floor_fee_amount", Decimal("99")),
    ):
        _, rejected = _run(
            _order(), [_assignment()], schedules, payments=[payment | {key: value}]
        )
        assert rejected["can_create_staff_payments"] is False
        assert any(error["code"].startswith("payment_") for error in rejected["errors"])


def test_total_hours_and_floor_fee_conservation_block_mismatch():
    schedules = [_schedule(date(2026, 7, 1)), _schedule(date(2026, 7, 2))]
    _, result = _run(
        _order(), [_assignment(actual_hours=Decimal("15"), floor_fee_allocated=Decimal("99"))], schedules
    )
    assert {"actual_hours_mismatch", "case_actual_hours_mismatch", "case_floor_fee_mismatch"} <= {
        error["code"] for error in result["errors"]
    }


def test_cursor_capabilities_and_database_errors_fail_closed():
    for cursor in (object(), type("NoFetchone", (), {"execute": lambda *a: None, "fetchall": lambda *a: []})()):
        with pytest.raises(ValueError, match="DictCursor"):
            reconcile_assignment_payroll_with_cursor(cursor, "CASE-1")

    class ExplodingCursor(Cursor):
        def execute(self, sql, params=()):
            raise RuntimeError("database unavailable")

    with pytest.raises(RuntimeError, match="database unavailable"):
        reconcile_assignment_payroll_with_cursor(ExplodingCursor(_order()), "CASE-1")


def test_pending_substitution_event_uses_existing_family_reconciliation_without_writes():
    leave_day = date(2026, 7, 3)
    assignments = [
        _assignment(1, actual_hours=Decimal("16"), floor_fee_allocated=Decimal("0.01")),
        _assignment(2, staff_id=8, actual_hours=Decimal("8"), floor_fee_allocated=Decimal("0.00")),
    ]
    schedules = [
        _schedule(date(2026, 7, 1), 1),
        _schedule(date(2026, 7, 2), 1),
        _schedule(leave_day, 1, id=90, is_work_day=False),
        _schedule(leave_day, 2, id=91, staff_id=8),
    ]
    pending = _event(90, leave_day) | {"event_key": "leave-case-1-20260703"}
    original = dict(pending)

    cursor, result = _run(
        _order(3, floor="0.01"),
        assignments,
        schedules,
        pending_substitution_event=pending,
    )

    assert result["can_create_staff_payments"] is True
    assert result["assignments"][0]["expected_family_floor_fee"] == Decimal("0.01")
    assert result["assignments"][1]["expected_family_floor_fee"] == Decimal("0.00")
    assert pending == original
    sql = "\n".join(statement for statement, _ in cursor.statements).upper()
    assert "INSERT INTO" not in sql and "UPDATE " not in sql and "DELETE FROM" not in sql


def test_pending_substitution_event_null_is_backward_compatible():
    schedules = [_schedule(date(2026, 7, 1)), _schedule(date(2026, 7, 2))]
    _, omitted = _run(_order(), [_assignment()], schedules)
    _, explicit_null = _run(
        _order(),
        [_assignment()],
        schedules,
        pending_substitution_event=None,
    )
    assert explicit_null == omitted


@pytest.mark.parametrize(
    ("prefix_id", "suffix_id", "expected_allocations"),
    [
        (2, 4, {1: "0.00", 2: "0.01", 3: "0.00", 4: "0.00"}),
        (2, None, {1: "0.00", 2: "0.00", 3: "0.01"}),
        (None, 4, {1: "0.00", 3: "0.01", 4: "0.00"}),
        (None, None, {1: "0.00", 3: "0.01"}),
    ],
)
def test_cancelled_original_pending_family_uses_explicit_lineage_and_remainder_priority(
    prefix_id, suffix_id, expected_allocations
):
    leave_day = date(2026, 7, 2)
    assignments = [
        _assignment(
            1,
            status="cancelled",
            actual_hours=Decimal("0"),
            floor_fee_allocated=Decimal("0"),
        ),
        _assignment(
            3,
            staff_id=8,
            actual_hours=Decimal("8"),
            floor_fee_allocated=(
                Decimal("0.01")
                if (prefix_id is None) != (suffix_id is None)
                else Decimal("0")
            ),
        ),
    ]
    schedules = [_schedule(leave_day, 1, id=90, is_work_day=False)]
    if prefix_id is not None:
        assignments.append(
            _assignment(
                prefix_id,
                actual_hours=Decimal("8"),
                floor_fee_allocated=(
                    Decimal("0.01") if suffix_id is not None else Decimal("0")
                ),
            )
        )
        schedules.append(_schedule(date(2026, 7, 1), prefix_id))
    if suffix_id is not None:
        assignments.append(
            _assignment(
                suffix_id,
                actual_hours=Decimal("8"),
                floor_fee_allocated=Decimal("0"),
            )
        )
        schedules.append(_schedule(date(2026, 7, 3), suffix_id))
    if prefix_id is None and suffix_id is None:
        assignments[1]["floor_fee_allocated"] = Decimal("0.01")
    schedules.append(_schedule(leave_day, 3, id=91, staff_id=8))
    pending = _event(
        90,
        leave_day,
        substitute_id=3,
        event_key="pending-split",
        prefix_assignment_id=prefix_id,
        suffix_assignment_id=suffix_id,
    )
    service_days = 1 + int(prefix_id is not None) + int(suffix_id is not None)

    _, result = _run(
        _order(service_days, floor="0.01"),
        assignments,
        schedules,
        pending_substitution_event=pending,
    )

    assert result["can_create_staff_payments"] is True
    details = {item["assignment_id"]: item for item in result["assignments"]}
    assert details[1]["actual_hours"] == Decimal("0")
    assert details[1]["service_salary"] == Decimal("0")
    assert {
        assignment_id: detail["expected_family_floor_fee"]
        for assignment_id, detail in details.items()
    } == {
        assignment_id: Decimal(amount)
        for assignment_id, amount in expected_allocations.items()
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"prefix_assignment_id": True},
        {"suffix_assignment_id": True},
        {"prefix_assignment_id": 1},
        {"prefix_assignment_id": 3},
        {"prefix_assignment_id": 2, "suffix_assignment_id": 2},
    ],
)
def test_pending_substitution_event_rejects_invalid_explicit_lineage_ids(overrides):
    pending = _event(90, date(2026, 7, 2), substitute_id=3, event_key="pending") | overrides
    with pytest.raises(ValueError, match="pending_substitution_event"):
        _run(_order(), [_assignment()], [], pending_substitution_event=pending)


def test_cancelled_original_rejects_forged_or_missing_replacement_lineage():
    day = date(2026, 7, 2)
    assignments = [
        _assignment(1, status="cancelled", actual_hours=Decimal("0"), floor_fee_allocated=Decimal("0")),
        _assignment(2, actual_hours=Decimal("8"), floor_fee_allocated=Decimal("50"), staff_id=99),
        _assignment(3, actual_hours=Decimal("8"), floor_fee_allocated=Decimal("50"), staff_id=8),
    ]
    schedules = [
        _schedule(day, 1, id=90, is_work_day=False),
        _schedule(date(2026, 7, 1), 2, staff_id=99),
        _schedule(day, 3, id=91, staff_id=8),
    ]
    pending = _event(
        90,
        day,
        substitute_id=3,
        event_key="pending-forged-lineage",
        prefix_assignment_id=2,
    )
    _, result = _run(
        _order(2),
        assignments,
        schedules,
        pending_substitution_event=pending,
    )
    assert "substitution_replacement_lineage_invalid" in {
        error["code"] for error in result["errors"]
    }


@pytest.mark.parametrize(
    "pending",
    [
        [],
        _event(90, date(2026, 7, 2)) | {"event_key": "event-1", "salary": "800"},
        _event(90, date(2026, 7, 2)) | {"event_key": "event-1", "original_assignment_id": True},
        _event(90, date(2026, 7, 2)) | {"event_key": "event-1", "substitute_assignment_id": True},
        _event(90, date(2026, 7, 2)) | {"event_key": "event-1", "original_schedule_id": True},
        _event(90, date(2026, 7, 2)) | {"event_key": "event-1", "case_no": "OTHER"},
        _event(90, date(2026, 7, 2)) | {"event_key": "event-1", "resolution_type": "leave_only"},
        _event(90, date(2026, 7, 2)) | {"event_key": "event-1", "substitute_assignment_id": 1},
    ],
)
def test_pending_substitution_event_rejects_noncanonical_payload(pending):
    with pytest.raises(ValueError, match="pending_substitution_event"):
        _run(_order(), [_assignment()], [], pending_substitution_event=pending)


@pytest.mark.parametrize(
    ("existing_event", "pending"),
    [
        (
            _event(90, date(2026, 7, 2)) | {"event_key": "event-1"},
            _event(90, date(2026, 7, 2), substitute_id=3) | {"event_key": "event-1"},
        ),
        (
            _event(90, date(2026, 7, 2)) | {"event_key": "existing"},
            _event(90, date(2026, 7, 3)) | {"event_key": "pending"},
        ),
        (
            _event(90, date(2026, 7, 2)) | {"event_key": "existing"},
            _event(92, date(2026, 7, 3)) | {"event_key": "pending"},
        ),
    ],
)
def test_pending_substitution_event_fails_closed_on_db_event_conflicts(existing_event, pending):
    leave_day = date(2026, 7, 2)
    assignments = [
        _assignment(1, actual_hours=Decimal("0"), floor_fee_allocated=Decimal("0")),
        _assignment(2, staff_id=8, actual_hours=Decimal("8"), floor_fee_allocated=Decimal("100")),
    ]
    schedules = [
        _schedule(leave_day, 1, id=90, is_work_day=False),
        _schedule(leave_day, 2, id=91, staff_id=8),
    ]
    if existing_event["event_key"] == pending["event_key"]:
        with pytest.raises(ValueError, match="event_key already exists"):
            _run(
                _order(1),
                assignments,
                schedules,
                events=[existing_event],
                pending_substitution_event=pending,
            )
    else:
        _, result = _run(
            _order(1),
            assignments,
            schedules,
            events=[existing_event],
            pending_substitution_event=pending,
        )
        assert result["can_create_staff_payments"] is False
        assert {
            "duplicate_substitution_event",
            "original_schedule_ownership_mismatch",
        } & {error["code"] for error in result["errors"]}
