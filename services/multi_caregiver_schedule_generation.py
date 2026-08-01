"""Generate assignment-owned daily schedules without touching legacy schedules."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from services.db_service import get_connection
from services.multi_caregiver_assignment_rules import (
    validate_non_overlapping_assignment_interval,
)


_WEEKDAY_NAMES = {
    "Monday": 0,
    "Tuesday": 1,
    "Wednesday": 2,
    "Thursday": 3,
    "Friday": 4,
    "Saturday": 5,
    "Sunday": 6,
}


def _as_date(value: Any, field_name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an ISO date") from exc
    raise ValueError(f"{field_name} is required")


def _weekly_rest_days(value: Any) -> set[int]:
    """Return the caregiver's configured weekly leave days.

    Existing records without a preference keep the legacy Sunday-rest default.
    Invalid configured JSON is rejected rather than silently changing payroll.
    """

    if value in (None, ""):
        return {6}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("weekly_rest_days must be a JSON array") from exc
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise ValueError("weekly_rest_days must be a JSON array")
    unknown = set(value) - set(_WEEKDAY_NAMES)
    if unknown:
        raise ValueError("weekly_rest_days contains an unsupported weekday")
    return {_WEEKDAY_NAMES[name] for name in value}


def _days_inclusive(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _assignment_row(cursor: Any, assignment_id: int) -> Mapping[str, Any]:
    cursor.execute(
        """SELECT a.id, a.case_no, a.staff_id, a.status,
                  a.assigned_start_date, a.assigned_end_date,
                  o.service_hours_per_day
             FROM case_staff_assignments a
             JOIN orders o ON o.case_no = a.case_no
            WHERE a.id = %s
            FOR UPDATE""",
        (assignment_id,),
    )
    assignment = cursor.fetchone()
    if assignment is None:
        raise ValueError("assignment does not exist")
    if assignment["status"] == "cancelled":
        raise ValueError("cancelled assignment cannot generate a schedule")
    return assignment


def _assert_assignment_is_unlocked(cursor: Any, assignment_id: int) -> None:
    cursor.execute(
        """SELECT id FROM staff_payments
            WHERE assignment_id = %s AND payment_status <> 'cancelled'
            LIMIT 1 FOR UPDATE""",
        (assignment_id,),
    )
    if cursor.fetchone() is not None:
        raise ValueError("assignment has a non-cancelled staff payment")

    cursor.execute(
        """SELECT d.id
             FROM staff_monthly_settlement_details d
             JOIN staff_monthly_settlements s ON s.id = d.settlement_id
            WHERE d.assignment_id = %s AND s.status <> 'cancelled'
            LIMIT 1 FOR UPDATE""",
        (assignment_id,),
    )
    if cursor.fetchone() is not None:
        raise ValueError("assignment has an active monthly settlement detail")

    cursor.execute(
        """SELECT id FROM actual_hours_adjustments
            WHERE assignment_id = %s
            LIMIT 1 FOR UPDATE""",
        (assignment_id,),
    )
    if cursor.fetchone() is not None:
        raise ValueError("assignment has actual-hours adjustments")


def _normalize_service_hours(service_hours_per_day: Any) -> Decimal:
    try:
        service_hours = Decimal(str(service_hours_per_day))
    except (TypeError, InvalidOperation, ValueError) as exc:
        raise ValueError("service_hours_per_day must be a finite positive decimal") from exc
    if not service_hours.is_finite() or service_hours <= 0:
        raise ValueError("service_hours_per_day must be a finite positive decimal")
    return service_hours


def _validate_assignment_id(assignment_id: object) -> int:
    if isinstance(assignment_id, bool) or not isinstance(assignment_id, int) or assignment_id < 1:
        raise ValueError("assignment_id must be a positive integer")
    return assignment_id


def _generate_assignment_schedule_with_cursor(cursor: Any, assignment_id: int) -> dict[str, Any]:
    """Generate rows using a caller-owned cursor without ending its transaction."""
    assignment_id = _validate_assignment_id(assignment_id)

    assignment = _assignment_row(cursor, assignment_id)
    start_date, end_date = validate_non_overlapping_assignment_interval(
        assignment["assigned_start_date"],
        assignment["assigned_end_date"],
        _load_case_assignments(cursor, assignment["case_no"]),
        candidate_assignment_id=assignment_id,
    )
    _assert_assignment_is_unlocked(cursor, assignment_id)

    cursor.execute(
        "SELECT weekly_rest_days FROM staff WHERE id = %s FOR UPDATE",
        (assignment["staff_id"],),
    )
    staff = cursor.fetchone()
    if staff is None:
        raise ValueError("assignment staff does not exist")
    rest_days = _weekly_rest_days(staff.get("weekly_rest_days"))

    cursor.execute(
        """SELECT holiday_date FROM holidays
            WHERE holiday_date BETWEEN %s AND %s
            FOR UPDATE""",
        (start_date, end_date),
    )
    holidays = {_as_date(row["holiday_date"], "holiday_date") for row in cursor.fetchall()}

    daily_hours = _normalize_service_hours(assignment["service_hours_per_day"])

    cursor.execute(
        """SELECT id, case_no, staff_id, assignment_id, work_date,
                  is_work_day, is_double_pay, notes
             FROM staff_schedule
            WHERE staff_id = %s AND work_date BETWEEN %s AND %s
            FOR UPDATE""",
        (assignment["staff_id"], start_date, end_date),
    )
    existing_by_date = {
        _as_date(row["work_date"], "work_date"): row for row in cursor.fetchall()
    }

    inserted: list[dict[str, Any]] = []
    work_day_count = 0
    for work_date in _days_inclusive(start_date, end_date):
        existing = existing_by_date.get(work_date)
        if existing is not None:
            if existing.get("assignment_id") != assignment_id:
                raise ValueError(f"staff already has a schedule on {work_date.isoformat()}")
            if existing.get("case_no") != assignment["case_no"]:
                raise ValueError(f"assignment schedule case mismatch on {work_date.isoformat()}")
            if bool(existing.get("is_work_day")):
                work_day_count += 1
            continue

        is_work_day = work_date.weekday() not in rest_days and work_date not in holidays
        notes = (
            "國定假日預設放假"
            if work_date in holidays
            else "週休預設放假" if work_date.weekday() in rest_days else None
        )
        cursor.execute(
            """INSERT INTO staff_schedule
                   (case_no, staff_id, assignment_id, work_date,
                    is_work_day, is_double_pay, notes)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                assignment["case_no"], assignment["staff_id"], assignment_id,
                work_date, is_work_day, False, notes,
            ),
        )
        inserted.append(
            {
                "case_no": assignment["case_no"],
                "staff_id": assignment["staff_id"],
                "assignment_id": assignment_id,
                "work_date": work_date,
                "is_work_day": is_work_day,
                "is_double_pay": False,
                "notes": notes,
            }
        )
        if is_work_day:
            work_day_count += 1

    actual_hours = daily_hours * work_day_count
    cursor.execute(
        "UPDATE case_staff_assignments SET actual_hours = %s WHERE id = %s",
        (actual_hours, assignment_id),
    )
    return {"assignment_schedule": inserted, "actual_hours": actual_hours}


def generate_assignment_schedule(assignment_id: int) -> dict[str, Any]:
    """Create missing daily rows for one formal service assignment."""
    assignment_id = _validate_assignment_id(assignment_id)
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            result = _generate_assignment_schedule_with_cursor(cursor, assignment_id)
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def generate_assignment_schedule_in_transaction(cursor: Any, assignment_id: int) -> dict[str, Any]:
    """Generate assignment-owned rows without starting or ending a transaction."""
    return _generate_assignment_schedule_with_cursor(cursor, assignment_id)


def _load_case_assignments(cursor: Any, case_no: str) -> list[Mapping[str, Any]]:
    cursor.execute(
        """SELECT id, status, assigned_start_date, assigned_end_date
             FROM case_staff_assignments
            WHERE case_no = %s
            FOR UPDATE""",
        (case_no,),
    )
    return cursor.fetchall()
