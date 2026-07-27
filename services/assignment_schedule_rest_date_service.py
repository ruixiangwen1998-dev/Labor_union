"""
================================================================================
檔案名稱: services/assignment_schedule_rest_date_service.py
功能說明: 以 assignment_id 為唯一權屬進行月嫂排休與順延完工日保存服務 (AssignmentScheduleRestDateService)
================================================================================
"""

from __future__ import annotations

import re
from datetime import datetime, date
from decimal import Decimal, ROUND_CEILING, InvalidOperation
from typing import Any, Dict, List, Iterable

from services.db_service import calculate_attendance_schedule, get_connection
from services.multi_caregiver_assignment_rules import validate_non_overlapping_assignment_interval


def _as_positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be a positive integer")
    if value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _as_date(value: Any, field_name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
            raise ValueError(f"{field_name} must be YYYY-MM-DD")
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d").date()
        except Exception as exc:
            raise ValueError(f"{field_name} must be YYYY-MM-DD") from exc
        if value != parsed.isoformat():
            raise ValueError(f"{field_name} must be YYYY-MM-DD")
        return parsed
    raise ValueError(f"{field_name} must be YYYY-MM-DD")


def _as_rest_date_string(value: Any, field_name: str) -> str:
    if value is None or not isinstance(value, str):
        raise ValueError(f"{field_name} must be a YYYY-MM-DD string")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        raise ValueError(f"{field_name} must be a YYYY-MM-DD string")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except Exception as exc:
        raise ValueError(f"{field_name} must be a YYYY-MM-DD string") from exc
    if value != parsed.isoformat():
        raise ValueError(f"{field_name} must be a YYYY-MM-DD string")
    return value


def _as_positive_decimal(value: Any, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a positive decimal")
    try:
        normalised = Decimal(str(value))
    except (TypeError, InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a positive decimal") from exc
    if normalised <= 0:
        raise ValueError(f"{field_name} must be a positive decimal")
    return normalised


def _normalise_rest_dates(rest_dates: Any) -> List[str]:
    if rest_dates is None:
        raise ValueError("rest_dates must be an array")
    if not isinstance(rest_dates, list):
        raise ValueError("rest_dates must be an array")
    if len(rest_dates) == 0:
        return []
    parsed: list[date] = []
    for item in rest_dates:
        parsed.append(_as_date(_as_rest_date_string(item, "rest_date"), "rest_date"))
    unique_dates = sorted(set(parsed))
    return [item.isoformat() for item in unique_dates]


def _snapshot_failure(status: str, message: str, error_code: str) -> Dict[str, Any]:
    return {
        "success": False,
        "status": status,
        "error_code": error_code,
        "message": message,
    }


def _load_case_assignments(cursor: Any, case_no: str) -> list[dict[str, Any]]:
    cursor.execute(
        """SELECT id, status, assigned_start_date, assigned_end_date
             FROM case_staff_assignments
            WHERE case_no = %s
            FOR UPDATE""",
        (case_no,),
    )
    return list(cursor.fetchall() or [])


def _assert_assignment_is_unlocked(cursor: Any, assignment_id: int) -> None:
    cursor.execute(
        """SELECT id FROM staff_payments
            WHERE assignment_id = %s AND payment_status <> 'cancelled'
            LIMIT 1 FOR UPDATE""",
        (assignment_id,),
    )
    if cursor.fetchone() is not None:
        raise ValueError("assignment is locked by non-cancelled staff payment")

    cursor.execute(
        """SELECT d.id
             FROM staff_monthly_settlement_details d
             JOIN staff_monthly_settlements s ON s.id = d.settlement_id
            WHERE d.assignment_id = %s AND s.status <> 'cancelled'
            LIMIT 1 FOR UPDATE""",
        (assignment_id,),
    )
    if cursor.fetchone() is not None:
        raise ValueError("assignment is locked by non-cancelled monthly settlement")

    cursor.execute(
        """SELECT id FROM actual_hours_adjustments
            WHERE assignment_id = %s
            LIMIT 1 FOR UPDATE""",
        (assignment_id,),
    )
    if cursor.fetchone() is not None:
        raise ValueError("assignment requires actual-hours review before changing schedule")


def save_assignment_rest_dates(assignment_id: int, rest_dates: Iterable[Any]) -> Dict[str, Any]:
    """
    更新特定 assignment_id 的排休與順延完工日。
    """
    try:
        assignment_id = _as_positive_int(assignment_id, "assignment_id")
    except ValueError as exc:
        return _snapshot_failure("validation_error", str(exc), "invalid_assignment_id")

    normalised_rest_dates = None
    try:
        normalised_rest_dates = _normalise_rest_dates(rest_dates)
    except ValueError as exc:
        return _snapshot_failure("validation_error", str(exc), "invalid_rest_dates")

    rest_dates_set = {_as_date(item, "rest_date") for item in normalised_rest_dates}
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT csa.id AS assignment_id, csa.case_no, csa.staff_id, csa.status,
                       csa.assigned_start_date AS assigned_start_date,
                       csa.planned_hours, o.service_hours_per_day,
                       o.actual_start_date AS order_actual_start_date,
                       o.start_date AS order_start_date
                FROM case_staff_assignments csa
                JOIN orders o ON csa.case_no = o.case_no
                WHERE csa.id = %s
                FOR UPDATE
                """,
                (assignment_id,),
            )
            assignment = cursor.fetchone()
            if assignment is None:
                return _snapshot_failure("not_found", "assignment_id does not exist", "assignment_not_found")
            if assignment.get("status") == "cancelled":
                return _snapshot_failure("validation_error", "assignment is cancelled", "assignment_cancelled")

            case_no = assignment["case_no"]
            staff_id = assignment["staff_id"]

            start_date = assignment.get("assigned_start_date") or assignment.get("order_actual_start_date") or assignment.get(
                "order_start_date"
            )
            try:
                assigned_start_date = _as_date(start_date, "assigned_start_date")
            except ValueError as exc:
                return _snapshot_failure("validation_error", str(exc), "assignment_start_date_invalid")

            try:
                planned_hours = _as_positive_decimal(assignment.get("planned_hours"), "planned_hours")
                service_hours_per_day = _as_positive_decimal(
                    assignment.get("service_hours_per_day"), "service_hours_per_day"
                )
                target_service_days = int(
                    (planned_hours / service_hours_per_day).quantize(Decimal("1"), rounding=ROUND_CEILING)
                )
            except ValueError as exc:
                return _snapshot_failure("validation_error", str(exc), "assignment_allocation_invalid")

            if target_service_days < 1:
                return _snapshot_failure(
                    "validation_error",
                    "assignment has no positive target work-days",
                    "assignment_target_zero",
                )

            try:
                _assert_assignment_is_unlocked(cursor, assignment_id)
            except ValueError as exc:
                return _snapshot_failure("locked", str(exc), "assignment_locked")

            calc_res = calculate_attendance_schedule(
                actual_start_date=assigned_start_date,
                target_service_days=target_service_days,
                service_mode="週休1日",
                custom_leave_dates=rest_dates_set,
                custom_holiday_rest_dates=None,
            )
            actual_end_date = calc_res.get("actual_end_date")
            if actual_end_date is None:
                return _snapshot_failure("validation_error", "cannot calculate actual_end_date", "schedule_calculation_failed")

            day_by_day = calc_res.get("day_by_day") or []
            if not isinstance(day_by_day, list) or not day_by_day:
                return _snapshot_failure("validation_error", "invalid schedule calculation result", "schedule_empty")

            case_assignments = _load_case_assignments(cursor, case_no)
            try:
                validate_non_overlapping_assignment_interval(
                    assigned_start_date,
                    actual_end_date,
                    case_assignments,
                    candidate_assignment_id=assignment_id,
                )
            except ValueError as exc:
                return _snapshot_failure("conflict", str(exc), "assignment_interval_overlap")

            work_dates = [item.get("date") for item in day_by_day]
            min_date, max_date = min(work_dates), max(work_dates)
            cursor.execute(
                """SELECT work_date, assignment_id, case_no
                     FROM staff_schedule
                    WHERE staff_id = %s
                      AND work_date BETWEEN %s AND %s
                    FOR UPDATE""",
                (staff_id, min_date, max_date),
            )
            for row in cursor.fetchall() or []:
                row_assignment_id = row.get("assignment_id")
                row_case_no = row.get("case_no")
                if row_assignment_id != assignment_id:
                    return _snapshot_failure(
                        "conflict",
                        "required schedule date is occupied by another assignment",
                        "schedule_conflict",
                    )
                if row_case_no != case_no:
                    return _snapshot_failure(
                        "conflict",
                        "schedule ownership case mismatch",
                        "schedule_case_conflict",
                    )

            cursor.execute("DELETE FROM staff_schedule WHERE assignment_id = %s", (assignment_id,))

            for item in day_by_day:
                work_date = item.get("date")
                is_work_day = item.get("is_work_day")
                notes = item.get("holiday_name") if not item.get("is_work_day") else None
                if notes is None and is_work_day is False:
                    notes = "排休"
                cursor.execute(
                    """
                    INSERT INTO staff_schedule
                        (assignment_id, case_no, staff_id, work_date, is_work_day, notes)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (assignment_id, case_no, staff_id, work_date, 1 if is_work_day else 0, notes),
                )

            cursor.execute(
                """
                UPDATE case_staff_assignments
                SET assigned_end_date = %s
                WHERE id = %s
                """,
                (actual_end_date, assignment_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("assignment could not be updated")

            conn.commit()
            return {
                "success": True,
                "status": "ok",
                "assignment_id": assignment_id,
                "case_no": case_no,
                "staff_id": staff_id,
                "actual_end_date": str(actual_end_date),
                "rest_dates": normalised_rest_dates,
            }

    except ValueError as exc:
        conn.rollback()
        return _snapshot_failure("validation_error", str(exc), "validation_error")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
