"""Adjust one formal caregiver assignment's existing daily schedule row."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from services.db_service import get_connection


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("work_date must be an ISO date") from exc
    raise ValueError("work_date must be an ISO date")


def _validate_request(
    assignment_id: Any,
    work_date: Any,
    is_work_day: Any,
    is_double_pay: Any,
    notes: Any,
) -> tuple[int, date, bool, bool, str | None]:
    if isinstance(assignment_id, bool) or not isinstance(assignment_id, int) or assignment_id < 1:
        raise ValueError("assignment_id must be a positive integer")
    if not isinstance(is_work_day, bool):
        raise ValueError("is_work_day must be a boolean")
    if not isinstance(is_double_pay, bool):
        raise ValueError("is_double_pay must be a boolean")
    if is_work_day is False and is_double_pay is True:
        raise ValueError("is_double_pay cannot be true when is_work_day is false")
    if notes is not None and not isinstance(notes, str):
        raise ValueError("notes must be a string or None")
    if isinstance(notes, str) and len(notes) > 255:
        raise ValueError("notes must be at most 255 characters")
    return assignment_id, _as_date(work_date), is_work_day, is_double_pay, notes


def adjust_assignment_schedule_day(
    assignment_id: int,
    work_date: date | str,
    is_work_day: bool,
    is_double_pay: bool,
    notes: str | None,
) -> dict[str, Any]:
    """Update one existing assigned day and recompute only that assignment's hours.

    The lock order is assignment/order, payment and settlement snapshots, target
    schedule row, then all schedule rows for the assignment.  No schedule rows
    are created, deleted, or reassigned by this operation.
    """
    assignment_id, target_date, is_work_day, is_double_pay, notes = _validate_request(
        assignment_id, work_date, is_work_day, is_double_pay, notes
    )

    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT CURRENT_DATE as current_date")
            current_date_row = cursor.fetchone()
            if current_date_row is None:
                raise ValueError("unable to load database current date")
            if isinstance(current_date_row, dict):
                db_current_date = current_date_row.get("current_date", current_date_row.get("CURRENT_DATE"))
            else:
                db_current_date = current_date_row[0] if current_date_row else None
            if db_current_date is None:
                raise ValueError("unable to load database current date")
            if isinstance(db_current_date, datetime):
                db_current_date = db_current_date.date()
            elif isinstance(db_current_date, str):
                db_current_date = date.fromisoformat(db_current_date)
            else:
                db_current_date = _as_date(db_current_date)
            if target_date < db_current_date:
                raise ValueError("work_date cannot be earlier than database current date")

            cursor.execute(
                """SELECT a.id, a.case_no, a.staff_id, a.assigned_start_date,
                          a.assigned_end_date, a.status, o.service_days,
                          o.service_hours_per_day
                   FROM case_staff_assignments a
                   JOIN orders o ON o.case_no = a.case_no
                   WHERE a.id = %s FOR UPDATE""",
                (assignment_id,),
            )
            assignment = cursor.fetchone()
            if not assignment:
                raise ValueError("assignment does not exist")
            if assignment["status"] == "cancelled":
                raise ValueError("cancelled assignment cannot be adjusted")

            start_date = assignment.get("assigned_start_date")
            end_date = assignment.get("assigned_end_date")
            if start_date is None or end_date is None:
                raise ValueError("assignment date range is incomplete")
            start_date = _as_date(start_date)
            end_date = _as_date(end_date)
            if not start_date <= target_date <= end_date:
                raise ValueError("work_date is outside the assignment date range")

            cursor.execute(
                """SELECT id FROM staff_payments
                   WHERE assignment_id = %s AND payment_status <> 'cancelled'
                   LIMIT 1 FOR UPDATE""",
                (assignment_id,),
            )
            if cursor.fetchone():
                raise ValueError("assignment with an active staff payment cannot be adjusted")

            cursor.execute(
                """SELECT smsd.id
                   FROM staff_monthly_settlement_details smsd
                   JOIN staff_monthly_settlements sms ON sms.id = smsd.settlement_id
                   WHERE smsd.assignment_id = %s AND sms.status <> 'cancelled'
                   LIMIT 1 FOR UPDATE""",
                (assignment_id,),
            )
            if cursor.fetchone():
                raise ValueError("assignment in an active monthly settlement cannot be adjusted")

            cursor.execute(
                """SELECT id
                   FROM actual_hours_adjustments
                   WHERE assignment_id = %s
                   LIMIT 1 FOR UPDATE""",
                (assignment_id,),
            )
            if cursor.fetchone():
                raise ValueError("assignment has manual actual hours adjustments and requires review")

            cursor.execute(
                """SELECT id, case_no, staff_id, assignment_id, work_date,
                          is_work_day, is_double_pay, notes
                   FROM staff_schedule
                   WHERE staff_id = %s AND work_date = %s FOR UPDATE""",
                (assignment["staff_id"], target_date),
            )
            schedule_day = cursor.fetchone()
            if not schedule_day:
                raise ValueError("assignment schedule day does not exist")
            if schedule_day.get("assignment_id") != assignment_id:
                raise ValueError("schedule day belongs to another assignment or requires review")
            if schedule_day.get("case_no") != assignment["case_no"]:
                raise ValueError("schedule day case does not match assignment")

            try:
                daily_hours = Decimal(str(assignment["service_hours_per_day"]))
            except (TypeError, ValueError, InvalidOperation) as exc:
                raise ValueError("service_hours_per_day must be a finite positive number") from exc
            if not daily_hours.is_finite() or daily_hours <= 0:
                raise ValueError("service_hours_per_day must be a finite positive number")
            service_days = assignment.get("service_days")
            if isinstance(service_days, bool) or not isinstance(service_days, int) or service_days < 1:
                raise ValueError("service_days must be a positive integer")
            order_planned_hours = daily_hours * service_days

            cursor.execute(
                """UPDATE staff_schedule
                   SET is_work_day = %s, is_double_pay = %s, notes = %s
                   WHERE id = %s AND assignment_id = %s""",
                (is_work_day, is_double_pay, notes, schedule_day["id"], assignment_id),
            )

            cursor.execute(
                """SELECT id, is_work_day
                   FROM staff_schedule
                   WHERE assignment_id = %s FOR UPDATE""",
                (assignment_id,),
            )
            assignment_days = cursor.fetchall()
            actual_hours = sum(
                (1 for row in assignment_days if bool(row.get("is_work_day"))),
                0,
            ) * daily_hours
            cursor.execute(
                "UPDATE case_staff_assignments SET actual_hours = %s WHERE id = %s",
                (actual_hours, assignment_id),
            )
            cursor.execute(
                """SELECT id AS assignment_id, actual_hours
                   FROM case_staff_assignments
                   WHERE case_no = %s AND status <> 'cancelled'
                   ORDER BY id FOR UPDATE""",
                (assignment["case_no"],),
            )
            case_actual_hours = Decimal("0")
            for row in cursor.fetchall() or []:
                value = row.get("actual_hours")
                try:
                    value = Decimal(str(value))
                except (TypeError, ValueError, InvalidOperation) as exc:
                    raise ValueError("case assignment actual_hours must be a finite non-negative number") from exc
                if not value.is_finite() or value < 0:
                    raise ValueError("case assignment actual_hours must be a finite non-negative number")
                case_actual_hours += value
            if case_actual_hours != order_planned_hours:
                raise ValueError(
                    "case assignment actual-hours total does not match order planned hours"
                )

        connection.commit()
        return {
            "adjusted_schedule_day": {
                **schedule_day,
                "is_work_day": is_work_day,
                "is_double_pay": is_double_pay,
                "notes": notes,
            },
            "actual_hours": actual_hours,
            "order_planned_hours": order_planned_hours,
            "case_actual_hours": case_actual_hours,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
